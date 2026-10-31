"""Neon budget (docs/neon-budget.md): stored answers, lean queries, the met hand-off, freshness.

Every answer here is computed from real recorded inputs (tests/fixtures/met, tests/fixtures/smoke
via test_met / test_smoke helpers) or from whatever the test database holds; nothing is invented.
Each test deletes the rows it writes.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import orjson
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from test_boundaries import areas  # noqa: F401  (fixture: recorded LGD district polygons)
from test_met import stored_runs  # noqa: F401  (fixture: the met job on recorded grids)

from blindspot.api import cache
from blindspot.db import init_db, session_scope
from blindspot.provenance import SourceRef


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


@pytest.fixture
def client() -> Any:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def _without_generated_at(body: bytes) -> bytes:
    return re.sub(rb'"generated_at":"[^"]*"', b"", body)


def _delete_bodies(*prefixes: str) -> None:
    with session_scope() as session:
        for p in prefixes:
            session.execute(text("DELETE FROM response_body WHERE key LIKE :p"), {"p": p + "%"})


# --- jsonb text ----------------------------------------------------------------------------------


def test_jsonb_text_matches_postgres() -> None:
    """The job renders jsonb text locally (cache.jsonb_text) and must get Postgres' own bytes."""
    from blindspot.db import _json_dumps

    values = [
        {"lon": 75.123456, "lat": 30.9, "t": 1730000000, "w": 1e-05, "big": 1.5e20, "neg0": -0.0,
         "f32": np.float32(0.1), "three": 3.0, "nan": float("nan"), "none": None, "yes": True},
        {"key with spaces": "a \"quoted\" \\ back\nslash\ttab\u0001", "µg/m³": "PM₂.₅ ਪੰਜਾਬ", "z": [], "aa": {}},
        [[75.1, 30.2], [1e-7, -2.5e-8], [123456789.123, 0.30000000000000004]],
        {"b": {"dd": 1, "c": 2, "aaa": [1, 2.5, "x"]}, "a": None},
    ]
    with session_scope() as session:
        for value in values:
            plain = orjson.loads(_json_dumps(value))
            expected = session.execute(text("SELECT CAST(:v AS jsonb)::text"), {"v": _json_dumps(value)}).scalar()
            assert cache.jsonb_text(plain) == expected


# --- stored answers --------------------------------------------------------------------------------


def test_stored_response_sends_the_bytes_a_live_answer_sends() -> None:
    from blindspot.api.responses import respond

    key = "test/neon-budget/stored"
    _delete_bodies(key)
    refs = [SourceRef(key="firms", label="NASA FIRMS", fetch_id=None)]
    payload = {"a": 1, "when": datetime(2026, 10, 31, 6, 0, tzinfo=UTC), "xs": [0.1, 2.5e-6],
               "nested": {"k": "µg/m³"}}
    calls = []

    def build() -> tuple[dict[str, Any], list[SourceRef], int]:
        calls.append(1)
        return payload, refs, 60

    try:
        with session_scope() as session:
            first = cache.stored_response(session, key, "stamp-1", build)
        with session_scope() as session:
            second = cache.stored_response(session, key, "stamp-1", build)
        live = respond(payload, refs, max_age=60)
        assert len(calls) == 1  # the second answer came from response_body
        assert _without_generated_at(first.body) == _without_generated_at(second.body) == _without_generated_at(live.body)
        assert second.headers["cache-control"] == live.headers["cache-control"] == "public, max-age=60"
        assert orjson.loads(second.body)["generated_at"].endswith("Z")
        with session_scope() as session:  # new data: computed again and replaced
            cache.stored_response(session, key, "stamp-2", build)
            assert len(calls) == 2
            row = session.execute(text("SELECT stamp, codec FROM response_body WHERE key = :k"), {"k": key}).one()
            assert tuple(row) == ("stamp-2", "xz")
        with session_scope() as session:  # a damaged row is never served
            session.execute(text("UPDATE response_body SET body = 'not xz' WHERE key = :k"), {"k": key})
        with session_scope() as session:
            again = cache.stored_response(session, key, "stamp-2", build)
        assert len(calls) == 3 and _without_generated_at(again.body) == _without_generated_at(live.body)
    finally:
        _delete_bodies(key)


def test_live_sources_are_recomputed_and_never_stored() -> None:
    key = "test/neon-budget/live-sources"
    _delete_bodies(key)
    current = {"ref": SourceRef(key="firms", label="NASA FIRMS", fetch_id=1)}

    def build() -> tuple[dict[str, Any], list[SourceRef], int]:
        return {"n": 5}, [current["ref"]], 30

    try:
        with session_scope() as session:
            cache.stored_response(session, key, "s", build, live_sources=lambda: [current["ref"]])
            stored = session.execute(text("SELECT sources FROM response_body WHERE key = :k"), {"k": key}).scalar()
        assert stored is None
        current["ref"] = SourceRef(key="firms", label="NASA FIRMS", fetch_id=2)  # a newer fetch
        with session_scope() as session:
            r = cache.stored_response(session, key, "s", build, live_sources=lambda: [current["ref"]])
        assert orjson.loads(r.body)["sources"][0]["fetch_id"] == 2
    finally:
        _delete_bodies(key)


def test_pruning_pictures_never_touches_stored_answers() -> None:
    """imagery.refresh_featured prunes rendered_asset; response_body is another table."""
    from blindspot.models.assets import prune_assets, prune_bodies, save_body

    key = "test/neon-budget/prune"
    assert save_body(key, "s", "xz", b"\x00", 1)
    try:
        prune_assets(keep=[], keep_days=0)
        with session_scope() as session:
            assert session.execute(text("SELECT count(*) FROM response_body WHERE key = :k"), {"k": key}).scalar() == 1
            session.execute(text("UPDATE response_body SET last_used_at = now() - interval '5 days' WHERE key = :k"),
                            {"k": key})
        dry = prune_bodies(keep_days=3, dry_run=True)
        assert dry["removed"] >= 1
        with session_scope() as session:
            assert session.execute(text("SELECT count(*) FROM response_body WHERE key = :k"), {"k": key}).scalar() == 1
        prune_bodies(keep_days=3)
        with session_scope() as session:
            assert session.execute(text("SELECT count(*) FROM response_body WHERE key = :k"), {"k": key}).scalar() == 0
    finally:
        _delete_bodies(key)


def test_table_stamp_follows_every_change() -> None:
    with session_scope() as session:
        session.execute(text("CREATE TABLE IF NOT EXISTS neon_budget_stamp_probe (id int, v int)"))
    try:
        stamps = []
        for sql in ("INSERT INTO neon_budget_stamp_probe VALUES (1, 1)", "INSERT INTO neon_budget_stamp_probe VALUES (2, 1)",
                    "UPDATE neon_budget_stamp_probe SET v = 2 WHERE id = 1", "DELETE FROM neon_budget_stamp_probe WHERE id = 2",
                    "DELETE FROM neon_budget_stamp_probe"):
            with session_scope() as session:
                stamps.append(cache.table_stamp(session, "neon_budget_stamp_probe"))
                session.execute(text(sql))
        with session_scope() as session:
            stamps.append(cache.table_stamp(session, "neon_budget_stamp_probe"))
        assert len(set(stamps[:5])) == 5  # every change gives a new stamp
        assert stamps[5] == stamps[0]  # empty again: the same (empty) answer
    finally:
        with session_scope() as session:
            session.execute(text("DROP TABLE IF EXISTS neon_budget_stamp_probe"))


# --- smoke: the job stores the website's answers -------------------------------------------------------


def test_smoke_job_stores_the_answers_the_routes_send(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_smoke import recorded_fires, recorded_members, recorded_receptors

    from blindspot.api.routers import smoke as routes
    from blindspot.jobs import smoke as job
    from blindspot.models.smoke import SmokeRun

    members, fires, receptors = recorded_members(), recorded_fires(), recorded_receptors()[:3]
    run_at = datetime.fromtimestamp(fires.t.max(), UTC) + timedelta(hours=2)
    forward = job.forward_run(members, receptors, fires, None, None, run_at, seed=7)
    backs = job.back_runs(members, receptors, fires, None, None, None, run_at, seed=7)
    monkeypatch.setattr(job, "prune", dict)  # never touch rows other tests own
    common = {"met_run_ids": [], "notes": ["test: recorded inputs"],
              "params": {"method": job.method_params(), "sources": [], "members": ["best_match", "gfs_seamless"],
                         "runtime_ms": 1}}
    ids = job.store(forward, backs, common, datetime.now(UTC))
    created = [ids["forward"], *ids["back"].values()]
    try:
        result = job.store_bodies(ids, forward, backs, common)
        assert "not_stored" not in result, result  # every column matched Postgres' own text
        assert result["stored"] == len(job.FORWARD_VARIANTS) + len(backs)
        city = backs[0]["city"]
        for path, params in (("/api/smoke/forward", {"max_trails": 600, "stride": 2}), ("/api/smoke/forward", {}),
                             ("/api/smoke/back", {"city": city})):
            routes._cache.clear()
            before = cache.stats()["stored_hits"]
            stored = client.get(path, params=params, headers={"Accept-Encoding": "identity"})
            assert stored.status_code == 200 and cache.stats()["stored_hits"] == before + 1, path
            with session_scope() as session:  # the same answer computed live from the row
                run = routes.load_run(session, ids["forward"] if path.endswith("forward") else ids["back"][city])
            if path.endswith("forward"):
                payload, sources, max_age = routes.forward_payload(run, params.get("max_trails"), params.get("stride", 1))
            else:
                payload, sources, max_age = routes.back_payload(run, 72, 72, None, 1)
            from blindspot.api.responses import respond

            live = respond(payload, sources, max_age=max_age)
            assert _without_generated_at(stored.content) == _without_generated_at(live.body), (path, params)
    finally:
        with session_scope() as session:
            session.execute(delete(SmokeRun).where(SmokeRun.id.in_(created)))
        _delete_bodies(*(f"smoke/forward/{ids['forward']}", *(f"smoke/back/{i}" for i in ids["back"].values())))
        routes._cache.clear()


def test_receptor_summaries_come_from_sql_unchanged(client: TestClient) -> None:
    """back_summaries picks the summary fields in SQL; they equal the whole summary's."""
    from blindspot.api.routers.smoke import SUMMARY_FIELDS, back_summaries
    from blindspot.models.smoke import latest_back_summaries

    with session_scope() as session:
        full = {r["city"]: r for r in latest_back_summaries(session)}
        slim = back_summaries(session)
    assert {r["city"] for r in slim} == set(full)
    for r in slim:
        whole = full[r["city"]]
        assert r["run_at"] == whole["run_at"]
        assert r["summary"] == {k: (whole["summary"] or {}).get(k) for k in SUMMARY_FIELDS}
        assert (r["receptors"] or [None])[0] == (whole["receptors"] or [None])[0]
        assert r["params"]["sources"] == (whole["params"] or {}).get("sources")


# --- met: the smoke job reads the grids met just stored, from memory ------------------------------------


def test_smoke_reads_the_grids_met_just_stored(stored_runs) -> None:  # noqa: F811
    from blindspot.jobs import met as met_job
    from blindspot.models.met import MetRun, load_level, row_at

    ids, _ = stored_runs
    assert met_job.KEEP_RUNS == 1
    with session_scope() as session:
        for run_id in ids:
            run = session.get(MetRun, run_id)
            held = met_job.stored_level(run_id)
            assert held is not None
            times, arrays = load_level(session, run, "bl", ("u", "v"))
            assert np.array_equal(held[0], times)
            for c in ("u", "v"):
                assert held[1][c].dtype == arrays[c].dtype and np.array_equal(held[1][c], arrays[c], equal_nan=True)
            at = datetime.fromtimestamp(int(times[40]), UTC) + timedelta(minutes=20)
            mine, theirs = met_job.stored_surface_at(run_id, at), row_at(session, run, "10m", at)
            assert mine.valid_at == theirs.valid_at and list(mine.blh) == list(theirs.blh)
    assert met_job.stored_level(-12345) is None


# --- scars: a case file reads four grid cells per hour, not 72 whole grids --------------------------------


def test_case_file_weather_reads_four_cells_and_gets_the_same_numbers(stored_runs) -> None:  # noqa: F811
    from blindspot.api.routers import scars

    _, grids = stored_runs
    g = grids["best_match"]
    t0 = datetime.fromtimestamp(int(g.times[30]), UTC)
    t1 = datetime.fromtimestamp(int(g.times[60]), UTC)
    lon_max = g.spec.lon0 + (g.spec.nx - 1) * g.spec.dlon
    lat_max = g.spec.lat0 + (g.spec.ny - 1) * g.spec.dlat
    points = [(75.85, 30.9), (g.spec.lon0, g.spec.lat0), (lon_max, lat_max), (lon_max - 0.3, g.spec.lat0 + 0.7)]
    with session_scope() as session:
        for lon, lat in points:
            row = {"lon": lon, "lat": lat, "burn_after": t0, "burn_before": t1}
            slim = scars._grid_corners(session, row)
            whole = session.execute(text("""
                SELECT DISTINCT ON (valid_at) valid_at, run_at, lon0, lat0, dlon, dlat, nx, ny, u, v, blh, t, precip
                FROM met_grid WHERE level = '10m' AND model = 'best_match' AND valid_at >= :a AND valid_at <= :b
                ORDER BY valid_at, run_at DESC"""), {"a": t0, "b": t1}).mappings().all()
            assert len(slim) == len(whole) > 0
            for a, b in zip(slim, whole, strict=True):
                assert a["valid_at"] == b["valid_at"]
                fx, fy = (lon - b["lon0"]) / b["dlon"], (lat - b["lat0"]) / b["dlat"]
                assert (a["i0"], a["j0"]) == (math.floor(fx), math.floor(fy))
                for f in ("u", "v", "blh", "t", "precip"):
                    assert scars._bilinear(a[f], b["nx"], fx, fy) == scars._bilinear(b[f], b["nx"], fx, fy), f


# --- the headline: freshness and errors ------------------------------------------------------------------


def test_freshness_flags_a_feed_that_stopped() -> None:
    from blindspot.analytics import headline

    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    status = {"firms_nrt": {"last_success_at": now - timedelta(hours=1), "last_run_at": now - timedelta(hours=1)},
              "air": {"last_success_at": now - timedelta(hours=5), "last_run_at": now - timedelta(minutes=30)}}
    fires = headline.freshness("fires_24h", status, now, now - timedelta(hours=2))
    assert fires["stale"] is False and fires["job"] == "firms_nrt" and fires["newest_data_at"] == now - timedelta(hours=2)
    air = headline.freshness("delhi", status, now, None)
    assert air["stale"] is True  # ran 30 min ago but last succeeded 5 h ago
    assert headline.freshness("gk2a_evening", status, now, None)["stale"] is True  # never ran
    assert {job for job, _ in headline.FEEDS.values()} >= {"firms_nrt", "air", "geostationary"}


def test_headline_blocks_carry_freshness_and_errors_are_not_cached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blindspot.analytics import headline

    r = client.get("/api/stats/headline")
    assert r.status_code == 200
    body = r.json()
    for block in headline.FEEDS:
        if "could not compute" not in str(body[block].get("reason", "")):
            assert set(body[block]["freshness"]) >= {"job", "last_success_at", "newest_data_at", "stale"}, block
    assert "s-maxage" in r.headers["cache-control"]

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(headline, "fires_24h", broken)
    cache.clear()
    r = client.get("/api/stats/headline")
    assert r.status_code == 200 and r.json()["fires_24h"]["available"] is False
    # one block failed: shared caches keep it a minute (retried soon, but not by every poll)
    assert r.headers["cache-control"] == "public, max-age=30, s-maxage=60"
    assert "_failed" not in r.json()


def test_headline_air_reads_one_city_and_matches_the_full_listing() -> None:
    from blindspot.api.routers.air import air_city, air_latest

    at = datetime.now(UTC)
    with session_scope() as session:
        payload, refs_all = air_latest(session, at)
        for city in {r["city"] for r in payload["receptors"]} | {"Delhi"}:
            entry, refs = air_city(session, city, at)
            expected = next((r for r in payload["receptors"] if r["city"].lower() == city.lower()), None)
            assert entry == expected, city
            assert refs == refs_all


# --- boundaries relabelling ------------------------------------------------------------------------------


def test_fill_admin_leaves_rows_that_are_already_right(areas: list[int]) -> None:  # noqa: F811
    from blindspot.geo import fill_admin

    with session_scope() as session:
        session.execute(text("CREATE TEMP TABLE probe (geom geometry(Geometry, 4326), state text, district text)"))
        session.execute(text("""
            INSERT INTO probe SELECT ST_PointOnSurface(geom), NULL, NULL FROM boundary
            WHERE id = ANY(:ids) AND level = 'district' AND name <> 'Hisar' ORDER BY id LIMIT 3"""),
            {"ids": areas})
        first = fill_admin(session, "probe", only_missing=False)
        assert first >= 3
        assert fill_admin(session, "probe", only_missing=False) == 0  # nothing to change: nothing rewritten
        session.execute(text("UPDATE probe SET district = 'wrong' WHERE ctid = (SELECT min(ctid) FROM probe)"))
        assert fill_admin(session, "probe", only_missing=False) >= 1
        session.execute(text("DROP TABLE probe"))


# --- repair (review findings) ------------------------------------------------------------------------------


def test_code_version_ignores_line_endings(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The API is deployed from a Windows tree (CRLF) and the jobs run from a Linux checkout (LF):
    the same source must give the same stamp, or the job's pre-stored bodies are never served."""
    import importlib
    import sys

    source = 'X = 1\n\ndef f():\n    return "a"\n'
    module = tmp_path / "nb_line_endings_probe.py"
    monkeypatch.syspath_prepend(str(tmp_path))

    def version(text_: str) -> str:
        module.write_bytes(text_.encode())
        cache._code_hashes.clear()
        sys.modules.pop("nb_line_endings_probe", None)
        importlib.invalidate_caches()
        return cache.code_version("nb_line_endings_probe")

    try:
        lf, crlf = version(source), version(source.replace("\n", "\r\n"))
        changed = version(source.replace("X = 1", "X = 2"))
    finally:
        cache._code_hashes.clear()
        sys.modules.pop("nb_line_endings_probe", None)
    assert lf == crlf  # CRLF and LF: one stamp
    assert changed != lf  # a real change still counts
    assert cache.source_text(b"a\r\nb\rc\n") == b"a\nb\nc\n"


def test_a_stale_row_is_reported_without_its_body() -> None:
    from blindspot.models.assets import probe_body, save_body

    key = "test/neon-budget/probe"
    _delete_bodies(key)
    try:
        with session_scope() as session:
            assert probe_body(session, key, "s1") == (None, False)  # never stored
        assert save_body(key, "s1", "xz", cache._compress(b'{"a":1}'), 7)
        with session_scope() as session:
            hit, known = probe_body(session, key, "s1")
            assert known is True and hit is not None
            assert cache._decompress(hit["codec"], hit["body"]) == b'{"a":1}'
            assert probe_body(session, key, "s2") == (None, True)  # stored for other data: no body sent
    finally:
        _delete_bodies(key)


def test_novel_heavy_queries_are_limited_and_known_ones_never(monkeypatch: pytest.MonkeyPatch) -> None:
    from blindspot.models import assets

    monkeypatch.setattr(cache, "NOVEL_LIMIT", 2)
    cache.clear()
    keys = [f"test/neon-budget/novel/{i}" for i in range(4)]
    _delete_bodies("test/neon-budget/novel/")
    calls: list[int] = []

    def build() -> tuple[dict[str, Any], list[SourceRef], int]:
        calls.append(1)
        return {"n": len(calls)}, [], 60

    try:
        with session_scope() as session:
            assert cache.stored_response(session, keys[0], "s", build, guard=True).status_code == 200
            assert cache.stored_response(session, keys[1], "s", build, guard=True).status_code == 200
            refused = cache.stored_response(session, keys[2], "s", build, guard=True)
            assert refused.status_code == 429 and len(calls) == 2  # nothing was read for it
            assert refused.headers["cache-control"] == "no-store" and int(refused.headers["retry-after"]) >= 1
            # known keys are always served: stored ones, and stored ones whose data changed
            assert cache.stored_response(session, keys[0], "s", build, guard=True).status_code == 200
            assert cache.stored_response(session, keys[1], "new-data", build, guard=True).status_code == 200
            # an unguarded (cheap) query is never refused
            assert cache.stored_response(session, keys[2], "s", build).status_code == 200
        with monkeypatch.context() as m:  # an unreadable store refuses nothing
            m.setattr(assets, "probe_body", lambda *a: (None, None))
            with session_scope() as session:
                assert cache.stored_response(session, keys[3], "s", build, guard=True).status_code == 200
        cache.clear()  # a new window
        with session_scope() as session:
            assert cache.stored_response(session, keys[3] + "x", "s", build, guard=True).status_code == 200
    finally:
        cache.clear()
        _delete_bodies("test/neon-budget/novel/")


def test_content_stamp_follows_only_the_rows_and_columns_an_answer_shows() -> None:
    """A season-replay day keeps its stored scars while the sentinel job rewrites matched_at on
    every row and adds newer scars; it changes when one of its own rows changes."""
    from datetime import date

    with session_scope() as session:
        session.execute(text("CREATE TABLE IF NOT EXISTS neon_budget_content_probe "
                             "(id int, day date, area float8, matched_at timestamptz)"))
        session.execute(text("DELETE FROM neon_budget_content_probe"))
        session.execute(text("INSERT INTO neon_budget_content_probe VALUES "
                             "(1, '2026-10-10', 2.5, now()), (2, '2026-10-12', 4.0, now())"))

    def stamp() -> str:
        with session_scope() as session:
            return cache.content_stamp(session, "neon_budget_content_probe", "id, day, area",
                                       "day <= :until", {"until": date(2026, 10, 12)})

    try:
        steps = {
            "UPDATE neon_budget_content_probe SET matched_at = now() + interval '1 day'": False,
            "INSERT INTO neon_budget_content_probe VALUES (3, '2026-10-20', 9.0, now())": False,
            "UPDATE neon_budget_content_probe SET area = 2.75 WHERE id = 1": True,
            "DELETE FROM neon_budget_content_probe WHERE id = 2": True,
            "INSERT INTO neon_budget_content_probe VALUES (4, '2026-10-11', 1.0, now())": True,
        }
        before = stamp()
        for sql, changes in steps.items():
            with session_scope() as session:
                session.execute(text(sql))
            after = stamp()
            assert (after != before) is changes, sql
            before = after
        with session_scope() as session:
            empty = cache.content_stamp(session, "neon_budget_content_probe", "id", "FALSE")
        assert empty == "neon_budget_content_probe:0:-"
    finally:
        with session_scope() as session:
            session.execute(text("DROP TABLE IF EXISTS neon_budget_content_probe"))


def test_the_scars_stamp_covers_every_column_the_list_renders() -> None:
    from blindspot.api.routers import scars

    stamped = {c.strip() for c in scars._STAMP_COLUMNS.split(",")}
    rendered: set[str] = set()
    for part in scars._FEATURE_COLUMNS.split(","):
        part = part.strip()
        rendered |= set(re.findall(r"\((\w+)\)", part)) if "(" in part else {part.split()[0]}
    assert rendered <= stamped, rendered - stamped
    assert {"geom", "model_version", "confidence"} <= stamped
    with session_scope() as session:  # the route's stamp runs on the real table
        assert scars.scars_stamp(session, until=datetime(2026, 10, 15, tzinfo=UTC).date(),
                                 min_confidence=0.5).startswith("burn_cluster:")


def test_smoke_variants_come_from_the_stored_full_answer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any max_trails / stride / hours variant is derived from the stored full answer (never the
    12 MB trails column again) and is byte-identical to computing it from the run."""
    from test_smoke import recorded_fires, recorded_members, recorded_receptors

    from blindspot.api.responses import respond
    from blindspot.api.routers import smoke as routes
    from blindspot.jobs import smoke as job
    from blindspot.models.smoke import SmokeRun, load_run

    members, fires, receptors = recorded_members(), recorded_fires(), recorded_receptors()[:2]
    run_at = datetime.fromtimestamp(fires.t.max(), UTC) + timedelta(hours=2)
    forward = job.forward_run(members, receptors, fires, None, None, run_at, seed=7)
    backs = job.back_runs(members, receptors, fires, None, None, None, run_at, seed=7)
    monkeypatch.setattr(job, "prune", dict)
    common = {"met_run_ids": [], "notes": ["test: recorded inputs"],
              "params": {"method": job.method_params(), "sources": [], "members": ["best_match", "gfs_seamless"],
                         "runtime_ms": 1}}
    ids = job.store(forward, backs, common, datetime.now(UTC))
    created = [ids["forward"], *ids["back"].values()]
    city = backs[0]["city"]
    cases = [
        ("/api/smoke/forward", {"max_trails": 599, "stride": 2}, lambda r: routes.forward_payload(r, 599, 2)),
        ("/api/smoke/forward", {"max_trails": 50}, lambda r: routes.forward_payload(r, 50, 1)),
        ("/api/smoke/forward", {"stride": 3}, lambda r: routes.forward_payload(r, None, 3)),
        ("/api/smoke/back", {"city": city, "hours": 24}, lambda r: routes.back_payload(r, 24, 24, None, 1)),
        ("/api/smoke/back", {"city": city, "hours": 120}, lambda r: routes.back_payload(r, 72, 120, None, 1)),
        ("/api/smoke/back", {"city": city, "stride": 2, "max_trails": 10},
         lambda r: routes.back_payload(r, 72, 72, 10, 2)),
    ]
    try:
        with session_scope() as session:  # what each variant must be, computed from the rows
            runs = {"forward": load_run(session, ids["forward"]), "back": load_run(session, ids["back"][city])}
        expected = []
        for path, _, make in cases:
            payload, sources, max_age = make(runs["forward" if path.endswith("forward") else "back"])
            expected.append(respond(payload, sources, max_age=max_age).body)
        assert "not_stored" not in job.store_bodies(ids, forward, backs, common)

        def no_rows(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("a variant read the run's rows")

        monkeypatch.setattr(routes, "load_run", no_rows)
        cache.clear()
        for (path, params, _), want in zip(cases, expected, strict=True):
            routes._cache.clear()
            got = client.get(path, params=params, headers={"Accept-Encoding": "identity"})
            assert got.status_code == 200, (path, params, got.text[:200])
            assert _without_generated_at(got.content) == _without_generated_at(want), (path, params)
        # a refused new variant is never kept by the route's in-process cache
        monkeypatch.setattr(cache, "NOVEL_LIMIT", 0)
        refused = client.get("/api/smoke/forward", params={"max_trails": 7})
        assert refused.status_code == 429 and "retry-after" in refused.headers
        monkeypatch.setattr(cache, "NOVEL_LIMIT", 20)
        assert client.get("/api/smoke/forward", params={"max_trails": 7}).status_code == 200
    finally:
        with session_scope() as session:
            session.execute(delete(SmokeRun).where(SmokeRun.id.in_(created)))
        _delete_bodies(*(f"smoke/forward/{ids['forward']}", *(f"smoke/back/{i}" for i in ids["back"].values())))
        routes._cache.clear()
        cache.clear()


def test_headline_during_an_outage_is_a_503_the_cdn_can_cover(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every block failing means the database is down: a 503 (same body), so the CDN's
    stale-if-error keeps showing the last good headline; nobody stores the error."""
    from blindspot.analytics import headline
    from blindspot.api.routers import stats

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("OperationalError: the database is suspended")

    for name in ("feed_status", "fires_24h", "season", "unseen_burns", "passes"):
        monkeypatch.setattr(headline, name, broken)
    monkeypatch.setattr(stats, "_air", broken)
    monkeypatch.setattr(stats, "_gk2a", broken)
    cache.clear()
    try:
        r = client.get("/api/stats/headline")
        assert r.status_code == 503
        assert r.headers["cache-control"] == "no-store" and r.headers["retry-after"] == "60"
        body = r.json()
        assert body["fires_24h"]["available"] is False and "_failed" not in body
        assert set(body) >= {"generated_at", "as_of", "fires_24h", "season", "delhi", "sources"}
    finally:
        cache.clear()


def test_scars_replay_days_are_never_refused_and_future_days_share_the_plain_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "NOVEL_LIMIT", 0)  # every novel costly key would be refused
    cache.clear()
    _delete_bodies("scars?")
    try:
        day = client.get("/api/scars", params={"limit": 2500, "min_confidence": 0.5, "until": "2026-10-01"})
        assert day.status_code == 200  # the website's replay family
        district = client.get("/api/scars", params={"limit": 2500, "min_confidence": 0.5, "district": "Sangrur"})
        assert district.status_code == 200
        odd = client.get("/api/scars", params={"limit": 2499, "min_confidence": 0.5})
        assert odd.status_code == 429 and odd.headers["cache-control"] == "no-store"
        small = client.get("/api/scars", params={"limit": 20, "until": "2026-10-03"})  # the agent's size
        assert small.status_code == 200
        plain = client.get("/api/scars", params={"limit": 2500, "min_confidence": 0.5})
        future = client.get("/api/scars", params={"limit": 2500, "min_confidence": 0.5, "until": "2099-01-01"})
        assert plain.status_code == future.status_code == 200
        assert _without_generated_at(future.content) == _without_generated_at(plain.content)
        with session_scope() as session:
            keys = set(session.execute(text("SELECT key FROM response_body WHERE key LIKE 'scars?%'")).scalars())
        assert not any("2099" in k for k in keys)  # one stored key for every future date
    finally:
        cache.clear()
        _delete_bodies("scars?")
