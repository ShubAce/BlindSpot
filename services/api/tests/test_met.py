"""Meteorology module: Open-Meteo parsing, level heights, boundary-layer wind, the met job's refresh
rules and storage, and the /api/met routes.

Fixtures are real recorded responses (tests/fixtures/met/): a 2-degree grid over 70-80E 26-34N for
both ensemble members (POSTed 2026-10-30), and Open-Meteo geocoding / model meta.json answers.
Tests delete only the rows they create.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from blindspot.analytics.trajectory import bl_mean_wind, pressure_level_heights
from blindspot.db import init_db, session_scope
from blindspot.models.met import MetGrid, MetRun
from blindspot.provenance import record_fetch
from blindspot.sources.openmeteo import GridSpec, ModelGrid, parse_grid

FIX = Path(__file__).parent / "fixtures" / "met"
MODELS = ("best_match", "gfs_seamless")


def load_fixture(model: str) -> dict:
    with gzip.open(FIX / f"openmeteo_{model}_2deg.json.gz", "rt", encoding="utf-8") as fh:
        return json.load(fh)


def fixture_grid(model: str, fetch_id: int | None = None) -> ModelGrid:
    data = load_fixture(model)
    spec = GridSpec(**data["spec"])
    times, fields, elevation, qa = parse_grid(data["response"], spec, data["hourly"])
    return ModelGrid(model, spec, times, fields, elevation, fetch_id,
                     datetime.fromisoformat(data["fetched_at"]), data["url"], dict(qa))


def meta_fixture() -> dict:
    return json.loads((FIX / "openmeteo_geocoding_and_meta.json").read_text(encoding="utf-8"))


# --- pure ---------------------------------------------------------------------------------------------


def test_grid_spec_covering_respects_point_budget():
    small = GridSpec.covering((70.0, 26.0, 80.0, 34.0))
    assert (small.dlon, small.nx, small.ny, small.n) == (0.5, 21, 17, 357)
    wide = GridSpec.covering((66.0, 24.0, 84.0, 36.0))  # SYNTHESIS domain
    assert (wide.dlon, wide.nx, wide.ny, wide.n) == (0.75, 25, 17, 425)
    assert wide.bbox == (66.0, 24.0, 84.0, 36.0)
    pts = wide.points()
    assert pts[0] == (24.0, 66.0) and pts[1] == (24.0, 66.75) and pts[wide.nx] == (24.75, 66.0)


def test_parse_grid_maps_locations_row_major():
    data = load_fixture("best_match")
    spec = GridSpec(**data["spec"])
    times, fields, elevation, qa = parse_grid(data["response"], spec, data["hourly"])
    assert times.size == 96 and np.all(np.diff(times) == 3600)
    assert fields["wind_u_component_925hPa"].shape == (96, spec.ny, spec.nx)
    assert elevation.shape == (spec.ny, spec.nx)
    # node (j=2, i=3) is location index 2*nx+3 in the request
    loc = data["response"][2 * spec.nx + 3]
    k = 40
    assert fields["boundary_layer_height"][k, 2, 3] == pytest.approx(loc["hourly"]["boundary_layer_height"][k])
    assert qa["max_cell_offset_deg"] < 0.2  # cell_selection=nearest keeps cells near the nodes
    assert not qa["nan_values"]
    with pytest.raises(ValueError):
        parse_grid(data["response"][:-1], spec, data["hourly"])
    with pytest.raises(ValueError):
        parse_grid(data["response"], spec, [*data["hourly"], "not_a_variable"])


def test_level_heights_on_real_fields():
    g = fixture_grid("best_match")
    z950, z925, z850 = pressure_level_heights(
        g.fields["surface_pressure"], g.fields["temperature_2m"], [950.0, 925.0, 850.0]
    )
    plains = g.fields["surface_pressure"] > 980.0  # low-lying nodes
    assert plains.any()
    # research section 1: ~350 / ~580 / ~1,300 m above the plains
    assert 250 < np.median(z950[plains]) < 500
    assert 450 < np.median(z925[plains]) < 750
    assert 1100 < np.median(z850[plains]) < 1600
    assert np.all(z950 < z925) and np.all(z925 < z850)
    highland = g.fields["surface_pressure"] < 800.0  # Ladakh / Tibet nodes: every level underground
    assert highland.any() and np.all(z850[highland] < 0)


def test_bl_mean_wind_exact_and_ignores_underground_levels():
    g = fixture_grid("best_match")
    f = g.fields
    heights = pressure_level_heights(f["surface_pressure"], f["temperature_2m"], [950.0, 925.0, 850.0])
    levels = ("10m", "950hPa", "925hPa", "850hPa")
    u = [f[f"wind_u_component_{lvl}"] for lvl in levels]
    v = [f[f"wind_v_component_{lvl}"] for lvl in levels]
    ub, vb = bl_mean_wind(u, v, [10.0, *heights], f["boundary_layer_height"])
    assert ub.shape == u[0].shape and np.isfinite(ub).all()
    # where every pressure level is underground the layer mean is the 10 m wind
    under = heights[2] < 10.0
    assert np.allclose(ub[under], u[0][under], atol=1e-4)
    # the mean lies between the smallest and largest wind of the column that it averages
    lo = np.minimum.reduce(u) - 1e-3
    hi = np.maximum.reduce(u) + 1e-3
    assert np.all((ub >= lo) & (ub <= hi))


def test_bl_mean_wind_matches_analytic_profile():
    # V(z) linear from 2 m/s at 10 m to 10 m/s at 1,010 m; h = 510 m: mean over [0,h] is exact
    shape = (1,)
    u = [np.full(shape, 2.0), np.full(shape, 4.0), np.full(shape, 6.0), np.full(shape, 10.0)]
    z = [10.0, np.full(shape, 260.0), np.full(shape, 510.0), np.full(shape, 1010.0)]
    ub, _ = bl_mean_wind(u, u, z, np.full(shape, 510.0))
    # 10 m * 2 + integral 10..510 of linear 2->6 (500 m * 4) = 2,020 over 510 m
    assert float(ub[0]) == pytest.approx((10 * 2.0 + 500 * 4.0) / 510.0, rel=1e-6)


def test_decide_refresh_rules(monkeypatch):
    from blindspot.jobs import met as met_job

    meta = meta_fixture()["meta"]

    def fake_meta(domain):
        d = meta[domain]
        to_iso = lambda s: datetime.fromtimestamp(s, UTC).isoformat().replace("+00:00", "Z")
        return {"domain": domain, "init": to_iso(d["last_run_initialisation_time"]),
                "available": to_iso(d["last_run_availability_time"])}

    monkeypatch.setattr(met_job, "fetch_model_meta", fake_meta)
    spec = GridSpec(70.0, 26.0, 2.0, 2.0, 6, 5)
    now = datetime(2026, 10, 30, 20, tzinfo=UTC)

    def run(hours_ago: float, runs: dict | None = None, s: GridSpec = spec) -> MetRun:
        return MetRun(model="best_match", run_at=now - timedelta(hours=hours_ago), lon0=s.lon0,
                      lat0=s.lat0, dlon=s.dlon, dlat=s.dlat, nx=s.nx, ny=s.ny, model_runs=runs or {})

    assert met_job.decide(None, spec, "best_match", now).reason == "no stored run"
    assert met_job.decide(run(1), spec, "best_match", now).fetch is False
    other = GridSpec(66.0, 24.0, 0.75, 0.75, 25, 17)
    assert met_job.decide(run(1, s=other), spec, "best_match", now).reason.startswith("the grid")
    current = {d: fake_meta(d) for d in ("ecmwf_ifs", "dwd_icon")}
    same = met_job.decide(run(6, current), spec, "best_match", now)
    assert same.fetch is False and "no new upstream" in same.reason
    stale = {**current, "dwd_icon": {"available": "2026-10-30T09:00:00Z"}}
    assert met_job.decide(run(6, stale), spec, "best_match", now).reason.startswith("new dwd_icon run")
    assert met_job.decide(run(10, current), spec, "best_match", now).fetch is True  # MAX_AGE


# --- database + routes ----------------------------------------------------------------------------


@pytest.fixture
def stored_runs(monkeypatch):
    """Run the met job on the recorded grids (no network); yield the run ids; delete them."""
    from blindspot.jobs import met as met_job

    init_db()
    started = datetime.now(UTC)
    grids = {}
    for model in MODELS:
        data = load_fixture(model)
        fetch_id = record_fetch("openmeteo", data["url"], ok=True, started_at=started,
                                http_status=200, meta={"fixture": True, "model": model})
        grids[model] = fixture_grid(model, fetch_id)
    spec = grids["best_match"].spec
    monkeypatch.setattr(met_job.GridSpec, "covering", classmethod(lambda cls, bbox, **kw: spec))
    monkeypatch.setattr(met_job, "latest_runs", lambda session, models=None: {})
    monkeypatch.setattr(met_job, "fetch_grid", lambda s, model, variables, **kw: grids[model])
    monkeypatch.setattr(met_job, "fetch_model_meta", lambda domain: {"domain": domain, "init": None,
                                                                     "available": None})
    monkeypatch.setattr(met_job, "PACE_S", 0.0)
    monkeypatch.setattr(met_job, "prune", lambda model, keep=3: 0)  # never touch other rows
    result = met_job.met()
    ids = [result["members"][m]["run_id"] for m in MODELS]
    yield ids, grids
    with session_scope() as session:
        session.execute(delete(MetRun).where(MetRun.id.in_(ids)))


def test_met_job_stores_both_members(stored_runs):
    ids, grids = stored_runs
    with session_scope() as session:
        for run_id, model in zip(ids, MODELS, strict=True):
            run = session.get(MetRun, run_id)
            assert run.model == model and run.n_times == 96 and (run.nx, run.ny) == (6, 5)
            counts = dict(session.execute(
                select(MetGrid.level, func.count()).where(MetGrid.run_id == run_id).group_by(MetGrid.level)
            ).all())
            assert counts == {lvl: 96 for lvl in ("10m", "950hPa", "925hPa", "850hPa", "bl")}
            row = session.scalars(select(MetGrid).where(MetGrid.run_id == run_id, MetGrid.level == "10m")
                                  .order_by(MetGrid.valid_at).limit(1)).one()
            assert len(row.u) == 30 and row.blh is not None and row.t is not None
            assert row.u[2 * 6 + 3] == pytest.approx(
                float(grids[model].fields["wind_u_component_10m"][0, 2, 3]), abs=0.006)


def test_met_routes(stored_runs):
    from blindspot.api.main import app

    ids, grids = stored_runs
    hour = datetime.fromtimestamp(int(grids["best_match"].times[60]), UTC)
    with TestClient(app) as client:
        r = client.get("/api/met/now", params={"lat": 28.65, "lon": 77.23, "t": hour.isoformat()})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["generated_at"].endswith("Z") and isinstance(body["sources"], list) and body["sources"]
        assert body["t"] == hour.isoformat().replace("+00:00", "Z")
        for key in ("wind_10m", "wind_925"):
            assert set(body[key]) >= {"speed_kmh", "from_deg"} and 0 <= body[key]["from_deg"] <= 360
        assert body["blh_m"] is not None and body["temp_2m_c"] is not None
        assert body["rain_mm_24h"] is not None
        assert body["inversion"] is None or isinstance(body["inversion"]["present"], bool)
        assert "gfs_seamless" in body["ensemble"]

        assert client.get("/api/met/now", params={"city": "No-Such-Town-Zz"}).status_code == 404
        assert client.get("/api/met/now").status_code == 400

        r = client.get("/api/met/wind-field", params={"t": hour.isoformat(), "level": "bl"})
        assert r.status_code == 200, r.text
        wf = r.json()
        assert wf["grid"] == {"lon0": 70.0, "lat0": 26.0, "dlon": 2.0, "dlat": 2.0, "nx": 6, "ny": 5}
        assert len(wf["u"]) == len(wf["v"]) == 30 and wf["level"] == "bl"
        assert wf["sources"] and wf["generated_at"].endswith("Z")
        assert client.get("/api/met/wind-field", params={"level": "300hPa"}).status_code == 400
