"""Tests for what keeps the hosted database small: compact burn-risk factors (analytics/risk.py,
jobs/risk.py store_scores, api/routers/risk.py), simplified boundaries (sources/boundaries.py) and
the daily `prune` job (jobs/prune.py).

Fixtures are real: harvest_cluster rows as the risk job stored them on 2026-10-30 and the LGD
district polygons of tests/fixtures/boundaries. Rows created here sit in the Indian Ocean or carry
test-only names, and are removed afterwards.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
import shapely
from fastapi.testclient import TestClient
from shapely.geometry import shape
from sqlalchemy import text

from blindspot.analytics import risk as model
from blindspot.db import session_scope
from blindspot.sources import boundaries as src

FX = Path(__file__).parent / "fixtures"
ROWS = json.loads((FX / "imagery_risk" / "risk_factors_rows.json").read_text(encoding="utf-8"))["rows"]


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.fixture
def client():
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


# --- compact risk factors -------------------------------------------------------------------------------


def test_recorded_rows_round_trip_exactly() -> None:
    """Every recorded row, split into the run's shared part and its own numbers and put back
    together, is the stored dict: same values, same key order, same bytes."""
    shared = orjson.loads(orjson.dumps(model.run_constants(ROWS[0]["risk_factors"])))
    for row in ROWS:
        stored = row["risk_factors"]
        assert model.run_constants(stored) == shared  # one run: the shared part is the same for all
        packed = {"run": 7, "v": model.pack_values(model.row_values(stored))}
        assert isinstance(packed["v"], str) and len(packed["v"]) < 120
        back = model.expand_factors(packed, shared, _t(row["harvested_after"]), _t(row["harvested_before"]))
        assert orjson.dumps(back) == orjson.dumps(stored)


def test_fresh_scores_round_trip_including_missing_factors() -> None:
    """Straight from score_fields (as the job holds them): history and rain missing on some fields,
    a field without harvest dates, a field with nothing nearby. Compared after the JSON round trip
    jsonb makes (datetimes become UTC strings)."""
    as_of = datetime(2026, 10, 30, 23, 13, 2, 192298, tzinfo=UTC)
    dates = [(as_of - timedelta(days=d + 5), as_of - timedelta(days=d)) for d in (2.3, 11.6, 27.0)] + [(None, None)]
    feats = [
        model.FieldFeatures(id=i, days_since_harvest=None if a is None else model.days_since_harvest(as_of, a, b)[0],
                            harvest_days_range=None if a is None else model.days_since_harvest(as_of, a, b)[1:],
                            fires_7d=f, unseen_burns_7d=u, burned_ha_7d=ha, rain_next_72h_mm=rain,
                            rain_past_24h_mm=None if rain is None else 0.4, fires_last_season=hist)
        for i, ((a, b), f, u, ha, rain, hist) in enumerate(zip(
            dates, (3, 0, 12, 1), (1, 0, 4, 0), (81.23, 0.0, 412.5, 2.07), (0.0, None, 13.2, 1.1), (10, None, 0, 4)))
    ]
    scored = model.score_fields(feats, as_of)
    for s in scored:
        s.factors["fetch_ids"] = {"rain": [4516], "history": [2910, 2911]}
    shared = orjson.loads(orjson.dumps(model.run_constants(scored[0].factors), option=orjson.OPT_UTC_Z))
    for s, (a, b) in zip(scored, dates, strict=True):
        stored = orjson.loads(orjson.dumps(s.factors, option=orjson.OPT_UTC_Z))
        packed = {"run": 1, "v": model.pack_values(model.row_values(s.factors))}
        assert model.expand_factors(packed, shared, a, b) == stored
    assert scored[1].factors["history"]["missing"] and scored[1].factors["dryness"]["missing"]
    assert scored[3].factors["timing"]["value"] is None


def test_full_factors_pass_through_and_compact_needs_its_run() -> None:
    legacy = ROWS[0]["risk_factors"]
    assert model.expand_factors(legacy, None, None, None) is legacy
    assert model.expand_factors(None, {"as_of": "2026-10-30T00:00:00Z"}, None, None) is None
    assert model.expand_factors({"run": 1, "v": "[1.0]"}, None, None, None) is None  # no run: nothing to invent
    assert not model.is_compact(legacy) and model.is_compact({"run": 1, "v": "[]"})


TEST_BBOX = "61.9,-6.1,62.3,-5.9"  # Indian Ocean: no other test's rows can be here


def _insert_fields(n: int) -> list[int]:
    t = datetime(2026, 10, 28, 5, 50, tzinfo=UTC)
    ids = []
    with session_scope() as s:
        cols = {c for (c,) in s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'harvest_cluster'"))}
        for i in range(n):
            lon = 62.0 + i / 100
            values = {
                "geom": f"SRID=4326;POLYGON(({lon} -6,{lon + 0.002} -6,{lon + 0.002} -5.998,{lon} -5.998,{lon} -6))",
                "centroid": f"SRID=4326;POINT({lon + 0.001} -5.999)", "area_ha": 3.0 + i, "n_px": 90, "res_m": 20,
                "mgrs_tile": "TESTC", "harvested_after": t - timedelta(days=3 + i),
                "harvested_before": t - timedelta(days=1 + i), "last_seen_unburnt": t, "scene_last": "test",
                "collection": "test", "village": f"Compactpur {i % 2}", "district": "Test district",
                "model_version": "test-compaction",
            }
            if "scope" in cols:
                values["scope"] = "test"
            keys = ", ".join(values)
            ids.append(s.execute(text(f"INSERT INTO harvest_cluster ({keys}) VALUES ({', '.join(':' + k for k in values)}) "
                                      "RETURNING id"), values).scalar_one())
    return ids


def test_store_scores_rewrites_only_changed_rows_and_the_route_rehydrates(client) -> None:
    from blindspot.jobs import risk as job

    ids = _insert_fields(4)
    runs: list[int] = []
    with session_scope() as s:  # the upstream calls the scores cite
        fetches = [s.execute(text("INSERT INTO source_fetch (source_key, url, ok) VALUES (:k, 'test', true) RETURNING id"),
                             {"k": k}).scalar_one() for k in ("firms", "firms", "openmeteo")]
    try:
        with session_scope() as s:
            fields = [f for f in job.load_fields(s) if f.id in ids]

        def score(as_of: datetime, rain: float) -> list[model.Scored]:
            near = {f.id: (2 + k, k, 10.5 * k) for k, f in enumerate(fields)}
            history = job.History(job.np.array([[f.lon, f.lat] for f in fields[:2]]), "2025-09-15/2025-11-30",
                                  fetches[:2], True)
            cells = {job._cell(f.lon, f.lat): (rain, 0.0) for f in fields}
            return job._score(fields, near, history, job.Rain(cells, fetches[2:]), as_of)

        as_of = datetime(2026, 10, 30, 23, 13, 2, 192298, tzinfo=UTC)
        first = score(as_of, 0.0)
        run1, changed1 = job.store_scores(first, as_of)
        runs.append(run1)
        assert changed1 == 4
        # Same inputs three hours on: the fields are inside their window (timing 1.0) and nothing
        # else moved, so no row is rewritten; days_since_harvest still follows the newer run.
        later = as_of + timedelta(hours=3)
        second = score(later, 0.0)
        run2, changed2 = job.store_scores(second, later)
        runs.append(run2)
        assert changed2 == 0
        third = score(later + timedelta(hours=3), 12.0)  # rain: dryness changes every field
        run3, changed3 = job.store_scores(third, later + timedelta(hours=3))
        runs.append(run3)
        assert changed3 == 4

        body = client.get("/api/risk", params={"bbox": TEST_BBOX, "limit": 10}).json()
        assert body["as_of"] == orjson.loads(orjson.dumps(later + timedelta(hours=3), option=orjson.OPT_UTC_Z))
        got = {f["id"]: f["properties"]["risk_factors"] for f in body["features"]["features"]}
        for s in third:
            assert got[s.id] == orjson.loads(orjson.dumps(s.factors, option=orjson.OPT_UTC_Z))
        assert set(fetches) <= {r["fetch_id"] for r in body["sources"]}  # the run's fetches are cited
        days = {f["id"]: f["properties"]["days_since_harvest"] for f in body["features"]["features"]}
        assert days == {s.id: s.factors["timing"]["days_since_harvest"] for s in third}
        with session_scope() as s:
            stored = s.execute(text("SELECT risk_factors FROM harvest_cluster WHERE id = ANY(:ids)"), {"ids": ids}).scalars().all()
        assert all(set(f) == {"run", "v"} and f["run"] == run3 for f in stored)
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM harvest_cluster WHERE id = ANY(:ids) AND model_version = 'test-compaction'"),
                      {"ids": ids})
            s.execute(text("DELETE FROM risk_run WHERE id = ANY(:ids)"), {"ids": runs})
            s.execute(text("DELETE FROM source_fetch WHERE id = ANY(:ids)"), {"ids": fetches})


# --- simplified boundaries ------------------------------------------------------------------------------


def _districts() -> dict[str, shapely.Geometry]:
    data = json.loads((FX / "boundaries" / "lgd_districts_extract.geojson").read_text(encoding="utf-8"))
    return {f["properties"]["dtname"]: shape(f["geometry"]) for f in data["features"]}


def test_simplified_polygons_keep_the_outline_exact() -> None:
    """Four real LGD districts (Sangrur, Jind, Hisar, Bhiwani) stand in for 'India': their union's
    outline is kept exact, the rest is simplified, and no point just outside the outline falls
    inside a simplified polygon (the rule that keeps Pakistani fires out of Indian counts)."""
    areas = _districts()
    bbox = (75.4, 28.5, 76.6, 30.4)  # the 'wind box': part of the outline only
    band = src.outline_band(list(areas.values()), bbox)
    tolerance = 0.002  # coarse, so the effect shows on these already-thinned fixtures
    simple = {name: src.simplify_area(g, band, tolerance) for name, g in areas.items()}
    for name, g in areas.items():
        inside = shapely.box(*bbox).intersection(band)
        # exact in the band, close to the original everywhere, fewer vertices overall
        assert shapely.symmetric_difference(g.intersection(inside), simple[name].intersection(inside)).area < 1e-12
        assert shapely.hausdorff_distance(g, simple[name]) <= tolerance + 1e-9
        assert shapely.get_num_coordinates(simple[name]) < shapely.get_num_coordinates(g)
    union = shapely.union_all(list(areas.values()))
    ring = shapely.intersection(union.buffer(0.00005).boundary, shapely.box(*bbox).buffer(-0.02))  # ~5 m outside
    outside = shapely.points(shapely.get_coordinates(shapely.segmentize(ring, 0.001)))
    assert len(outside) > 100
    for g in simple.values():
        assert not shapely.contains(g, outside).any()


def test_default_tolerance_is_the_measured_one() -> None:
    """0.0002 deg is what the re-labelling test of 11,730 detections allowed (deploy-data log)."""
    assert src.SIMPLIFY_DEG == 0.0002 and src.EXACT_BAND_DEG == 0.01
    g = _districts()["Sangrur"]
    assert shapely.hausdorff_distance(g, src.simplify_area(g)) <= src.SIMPLIFY_DEG + 1e-9


# --- prune ----------------------------------------------------------------------------------------------


def test_prune_counts_in_dry_run_then_deletes_only_what_nothing_needs() -> None:
    from blindspot.db import engine
    from blindspot.jobs.prune import run_prune

    # run_prune works on the whole database: only ever on the tests' own copy (conftest.py).
    if not (engine.url.database or "").endswith("_test"):
        pytest.skip("prune is only exercised on the *_test database")
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    made: dict[str, list[int]] = {}
    with session_scope() as s:
        def fetch(started):
            return s.execute(text("INSERT INTO source_fetch (source_key, url, started_at, ok) "
                                  "VALUES ('prunetest', 'test', :t, true) RETURNING id"), {"t": started}).scalar_one()

        f_json, f_column, f_free, f_recent = fetch(old), fetch(old), fetch(old), fetch(now)
        made["source_fetch"] = [f_json, f_column, f_free, f_recent]

        def met(run_at, fetch_id=None):
            return s.execute(text("""
                INSERT INTO met_run (model, run_at, t_start, t_end, n_times, step_s, lon0, lat0, dlon, dlat, nx, ny,
                                     levels, variables, fetch_id)
                VALUES ('prunetest', :t, :t, :t, 1, 3600, 70, 26, 0.5, 0.5, 1, 1, '{10m}', '{u}', :f) RETURNING id
            """), {"t": run_at, "f": fetch_id}).scalar_one()

        met_old, met_new = met(now - timedelta(hours=6)), met(now, f_column)
        made["met_run"] = [met_old, met_new]
        s.execute(text("INSERT INTO met_grid (run_id, model, run_at, valid_at, level, lon0, lat0, dlon, dlat, nx, ny, u, v) "
                       "VALUES (:r, 'prunetest', :t, :t, '10m', 70, 26, 0.5, 0.5, 1, 1, '{1}', '{1}')"),
                  {"r": met_old, "t": now})

        def smoke(run_at, params=None):
            return s.execute(text("INSERT INTO smoke_run (kind, city, run_at, t0, hours, params) "
                                  "VALUES ('back', 'Prunetest', :t, :t, 72, CAST(:p AS jsonb)) RETURNING id"),
                             {"t": run_at, "p": json.dumps(params) if params else None}).scalar_one()

        smoke_old = smoke(now - timedelta(hours=3))
        smoke_new = smoke(now, {"sources": [{"key": "test", "fetch_id": f_json}]})
        made["smoke_run"] = [smoke_old, smoke_new]
        made["job_run"] = [s.execute(text(
            "INSERT INTO job_run (name, trigger, started_at, finished_at, ok, duration_ms) "
            "VALUES ('prunetest', 'cli', :t, :t, true, 1) RETURNING id"), {"t": old}).scalar_one()]
    try:
        plan = run_prune(dry_run=True)
        assert plan["dry_run"] and plan["smoke_run"]["rows"] >= 1 and plan["met_run"]["rows"] >= 1
        assert plan["met_grid"]["rows"] >= 1 and plan["job_run"]["rows"] >= 1 and plan["source_fetch"]["rows"] >= 1
        with session_scope() as s:  # nothing deleted yet
            assert s.execute(text("SELECT count(*) FROM smoke_run WHERE id = ANY(:i)"), {"i": made["smoke_run"]}).scalar() == 2
        done = run_prune()
        assert not done["dry_run"]
        with session_scope() as s:
            def alive(table, ids):
                return set(s.execute(text(f"SELECT id FROM {table} WHERE id = ANY(:i)"), {"i": ids}).scalars())

            assert alive("smoke_run", made["smoke_run"]) == {smoke_new}
            assert alive("met_run", made["met_run"]) == {met_new}
            assert s.execute(text("SELECT count(*) FROM met_grid WHERE run_id = :r"), {"r": met_old}).scalar() == 0
            assert alive("job_run", made["job_run"]) == set()
            # referenced from JSON, referenced by a column, and recent stay; the old orphan goes
            assert alive("source_fetch", made["source_fetch"]) == {f_json, f_column, f_recent}
            assert not {"fire_detection", "burn_cluster", "harvest_cluster", "air_obs"} & done.keys()  # never touched
    finally:
        with session_scope() as s:
            for table in ("smoke_run", "met_run", "job_run", "source_fetch"):
                s.execute(text(f"DELETE FROM {table} WHERE id = ANY(:i)"), {"i": made.get(table, [])})


def test_load_neon_refuses_local_and_pooler_targets() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "infra" / "load_neon.py"
    spec = importlib.util.spec_from_file_location("load_neon", path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    assert "local" in loader.target_problem("postgresql://u:p@localhost:5433/blindspot")
    assert "local" in loader.target_problem("postgresql+psycopg://u:p@127.0.0.1/x")
    assert "pooler" in loader.target_problem("postgresql://u:p@ep-x-pooler.region.aws.neon.tech/db")
    assert loader.target_problem("postgresql://u:p@ep-x.region.aws.neon.tech/db?sslmode=require") is None
