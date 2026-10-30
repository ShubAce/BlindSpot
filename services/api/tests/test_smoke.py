"""Smoke particle model: integrator numerics, releases, back-run statistics, verdicts, payload limits,
storage and the /api/smoke routes.

Inputs are real recorded responses: the 2-degree Open-Meteo grids of tests/fixtures/met/, the
FIRMS NOAA-20 VIIRS South Asia 24 h file clipped to the wind box (tests/fixtures/smoke/), and
Open-Meteo geocoding answers for the receptor cities. Uniform-wind cases are analytic checks of
the integrator, not data. Tests delete only the rows they create.
"""

from __future__ import annotations

import csv
import gzip
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import orjson
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_met import fixture_grid, meta_fixture

from blindspot.analytics import trajectory as tj
from blindspot.db import init_db, session_scope
from blindspot.jobs import smoke as job
from blindspot.models.met import MetRun
from blindspot.models.smoke import SmokeRun

FIX = Path(__file__).parent / "fixtures" / "smoke"
LEVELS = ("10m", "950hPa", "925hPa", "850hPa")


def wind_for(model: str) -> tuple[tj.WindField, np.ndarray, np.ndarray]:
    """Boundary-layer mean wind of a recorded grid, computed exactly as the met job does."""
    g = fixture_grid(model)
    f = g.fields
    heights = tj.pressure_level_heights(f["surface_pressure"], f["temperature_2m"], [950.0, 925.0, 850.0])
    ub, vb = tj.bl_mean_wind([f[f"wind_u_component_{lv}"] for lv in LEVELS],
                             [f[f"wind_v_component_{lv}"] for lv in LEVELS],
                             [10.0, *heights], f["boundary_layer_height"])
    return tj.WindField.from_arrays(g.spec.as_dict(), g.times, ub, vb), g.times, f["boundary_layer_height"]


def recorded_fires() -> job.Fires:
    with gzip.open(FIX / "firms_J1_VIIRS_South_Asia_24h_metbox.csv.gz", "rt", encoding="utf-8") as fh:
        next(fh)  # provenance comment line
        rows = [r for r in csv.DictReader(fh) if r["confidence"] != "l" and float(r["frp"]) > 0]
    t = np.array([datetime.strptime(r["acq_date"] + r["acq_time"].zfill(4), "%Y-%m-%d%H%M")
                  .replace(tzinfo=UTC).timestamp() for r in rows])
    frp = np.array([float(r["frp"]) for r in rows])
    return job.Fires(
        id=np.arange(1, len(rows) + 1), t=t,
        lat=np.array([float(r["latitude"]) for r in rows]),
        lon=np.array([float(r["longitude"]) for r in rows]),
        frp=frp, dm=np.asarray(tj.fire_dry_matter_t(frp)),
        state=[None] * len(rows), district=[None] * len(rows), fetch_ids=[],
        stats={"used": len(rows)},
    )


def recorded_receptors() -> list[job.Receptor]:
    geo = meta_fixture()["geocoding"]
    out = []
    for name, answer in geo.items():
        best = max(answer["results"], key=lambda r: r.get("population") or 0)
        out.append(job.Receptor(name, best["latitude"], best["longitude"], best.get("admin2"),
                                best.get("admin1"), "geocoded"))
    return out


def recorded_members() -> list[job.Member]:
    members = []
    for k, model in enumerate(("best_match", "gfs_seamless")):
        wind, times, blh = wind_for(model)
        g = fixture_grid(model)
        run = MetRun(id=-(k + 1), model=model, run_at=datetime(2026, 10, 30, 17, 26, tzinfo=UTC),
                     lon0=g.spec.lon0, lat0=g.spec.lat0, dlon=g.spec.dlon, dlat=g.spec.dlat,
                     nx=g.spec.nx, ny=g.spec.ny, model_runs={}, fetch_id=None)
        member = job.Member(model, run, wind)
        member.blh_now = blh[60].ravel().astype(np.float64)
        members.append(member)
    return members


# --- integrator numerics -----------------------------------------------------------------------------


class Last:
    def record(self, r, t, lat, lon, active):
        self.lat, self.lon, self.active, self.r = lat.copy(), lon.copy(), active.copy(), r


def test_uniform_wind_moves_exactly():
    times = np.arange(0, 49 * 3600, 3600.0)
    grid = {"lon0": 60.0, "lat0": 20.0, "dlon": 0.5, "dlat": 0.5, "nx": 81, "ny": 41}
    u = np.full((times.size, 41, 81), 10.0, np.float32)
    wind = tj.WindField.from_arrays(grid, times, u, np.zeros_like(u))
    last = Last()
    tj.integrate(wind, np.array([30.0]), np.array([70.0]), np.array([0.0]),
                 t_start=0.0, t_end=24 * 3600.0, k_h=0.0, observers=[last])
    # 10 m/s for 24 h at 30N: 864 km / (6371 km * cos 30) rad = 8.9722 deg (research check)
    assert last.lon[0] - 70.0 == pytest.approx(8.9722, abs=1e-4)
    assert last.lat[0] == pytest.approx(30.0)


def test_round_trip_and_timestep_on_recorded_wind():
    wind, times, _ = wind_for("best_match")
    t0 = float(times[60])
    lat0 = np.array([30.25, 29.4, 31.0])  # Sangrur, Kaithal, Ludhiana area
    lon0 = np.array([75.84, 76.4, 75.9])
    back, forth = Last(), Last()
    tj.integrate(wind, lat0, lon0, np.full(3, t0), t_start=t0, t_end=t0 - 24 * 3600, k_h=0, observers=[back])
    tj.integrate(wind, back.lat, back.lon, np.full(3, t0 - 24 * 3600), t_start=t0 - 24 * 3600,
                 t_end=t0, k_h=0, observers=[forth])
    km = np.hypot((forth.lat - lat0) * tj.KM_PER_DEG, (forth.lon - lon0) * tj.KM_PER_DEG * np.cos(np.radians(lat0)))
    assert km.max() < 5.0  # research: 2 km after 72 h back and forth
    fine, coarse = Last(), Last()
    tj.integrate(wind, lat0, lon0, np.full(3, t0), t_start=t0, t_end=t0 - 36 * 3600, k_h=0, dt_s=300,
                 observers=[fine])
    tj.integrate(wind, lat0, lon0, np.full(3, t0), t_start=t0, t_end=t0 - 36 * 3600, k_h=0, observers=[coarse])
    drift = np.hypot((fine.lat - coarse.lat) * tj.KM_PER_DEG, (fine.lon - coarse.lon) * tj.KM_PER_DEG)
    assert drift.max() < 1.0


def test_back_exposure_matches_forward_smoke_index_in_scale():
    """Reciprocity: a fire's forward smoke index at a receptor vs the back run's exposure to it.
    Exact only for area-preserving flow; the 2-D layer-mean flow converges, so a factor ~2."""
    wind, times, _ = wind_for("best_match")
    t_f = float(times[40])
    big_t = t_f + 18 * 3600
    n = 3000
    rng = np.random.default_rng(5)
    last = Last()
    tj.integrate(wind, np.full(n, 30.25), np.full(n, 75.84), t_f + rng.uniform(0, tj.FIRE_DURATION_S, n),
                 t_start=t_f, t_end=big_t, seed=1, observers=[last])
    clat, clon = last.lat[last.active].mean(), last.lon[last.active].mean()
    series = tj.ReceptorSeries(np.array([clat]), np.array([clon]), np.full(n, 1.0 / n),
                               np.zeros(n, np.int8), np.zeros(n, np.int32), 1, tj.n_records(t_f, big_t))
    tj.integrate(wind, np.full(n, 30.25), np.full(n, 75.84), t_f + rng.uniform(0, tj.FIRE_DURATION_S, n),
                 t_start=t_f, t_end=big_t, seed=1, observers=[series])
    forward = float(series.total[0, -1])
    blat, blon = job._disk(clat, clon, n, tj.RECEPTOR_RADIUS_KM, rng)
    rec = int(round((big_t - (t_f + tj.FIRE_DURATION_S / 2)) / tj.RECORD_S))
    sources = tj.Sources(np.array([30.25]), np.array([75.84]), np.array([rec]), np.array([1.0]),
                         np.zeros(1, np.int32), np.zeros(1, np.int8))
    summary = tj.BackSummary(np.zeros(n, int), 1, None, None, sources, big_t)
    tj.integrate(wind, blat, blon, np.full(n, big_t), t_start=big_t, t_end=t_f - 3600, seed=2,
                 observers=[summary])
    back = float(summary.exposure[0])
    assert forward > 0.05 and back > 0.0
    assert 0.4 <= back / forward <= 1.6


# --- releases, verdicts, statistics ------------------------------------------------------------------


def test_scar_release_hours_follow_the_evening_profile():
    after = datetime(2026, 10, 23, 5, 40, tzinfo=UTC)  # S2 pass ~11:10 IST
    before = datetime(2026, 10, 28, 5, 40, tzinfo=UTC)
    hours, shares = tj.scar_release_hours(after, before, 0.0, 4e9)
    assert shares.sum() == pytest.approx(1.0)
    ist_hours = {(datetime.fromtimestamp(h, UTC) + tj.IST_OFFSET).hour for h in hours}
    assert ist_hours == set(tj.BURN_HOUR_SHARE_IST)
    assert hours.min() >= after.timestamp() and hours.max() + 3600 <= before.timestamp()
    assert len(hours) == 5 * len(tj.BURN_HOUR_SHARE_IST)  # 23..27 Oct afternoons
    peak = hours[np.argmax(shares)]
    assert (datetime.fromtimestamp(peak, UTC) + tj.IST_OFFSET).hour == 17  # ISRO SAC: ~17:00 IST
    part, part_shares = tj.scar_release_hours(after, before, before.timestamp() - 86400 * 2, before.timestamp())
    assert part_shares.sum() == pytest.approx(0.4)  # two of five days fall in the window


def test_emission_units():
    assert tj.fire_dry_matter_t(1.0) == pytest.approx(0.6624)  # 1 MW x 1800 s x 0.368 kg/MJ
    assert tj.scar_dry_matter_t(2.0) == pytest.approx(11.0)  # 2 ha x 5.5 t/ha


def test_verdict_rules():
    assert tj.verdict_hint([0.6, 0.5], [40.0, 12.0])[0] == "stubble_likely"
    assert tj.verdict_hint([0.01, 0.02], [0.0, 0.1])[0] == "stubble_unlikely"
    assert tj.verdict_hint([0.6, 0.5], [0.0, 0.1])[0] == "mixed"  # crossed the belt, no detected fire
    assert tj.verdict_hint([0.6, 0.5], [40.0, 0.1])[0] == "mixed"  # members disagree
    assert tj.verdict_hint([None, None], [None, None])[0] is None
    assert tj.verdict_hint([None], [0.0])[0] == "stubble_unlikely"


def test_lagged_correlations_find_the_lead():
    hours = np.arange(200, dtype=float)
    model = np.sin(hours / 9.0) + 1.5
    obs = np.r_[np.full(4, np.nan), model[:-4]] * 40.0 + 20.0  # obs lag the model by 4 h
    res = tj.lagged_correlations(model, obs, control=np.cos(hours / 50.0))
    assert res["best_lag"]["lag_h"] == 4 and res["best_lag"]["r"] == pytest.approx(1.0, abs=1e-6)
    assert res["lag0"]["n_eff"] is not None and res["lag0"]["n_eff"] < res["lag0"]["n"]
    assert "not statistically robust" in tj.describe_r(0.7, 5.0)


# --- full runs on recorded inputs --------------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded_runs():
    members = recorded_members()
    fires = recorded_fires()
    receptors = recorded_receptors()
    run_at = datetime.fromtimestamp(fires.t.max(), UTC) + timedelta(hours=2)
    forward = job.forward_run(members, receptors, fires, None, None, run_at, seed=7)
    backs = job.back_runs(members, receptors, fires, None, None, None, run_at, seed=7)
    return run_at, forward, backs, receptors


def test_forward_run_contract_and_browser_limits(recorded_runs):
    run_at, forward, _, receptors = recorded_runs
    assert forward["inputs"]["fires"] > 100 and forward["inputs"]["scars"] == 0
    trails = forward["trails"]
    assert 0 < len(trails) <= 2000
    assert {t["model"] for t in trails} == {"best_match", "gfs_seamless"}
    for trail in trails[:400]:
        assert set(trail) >= {"id", "path", "times", "weight", "origin"}
        assert len(trail["path"]) == len(trail["times"]) >= 2
        assert all(b - a >= tj.RECORD_S for a, b in zip(trail["times"], trail["times"][1:], strict=False))
        for lon, lat in trail["path"]:
            assert lon == round(lon, 4) and lat == round(lat, 4)
            assert 70 <= lon <= 80 and 26 <= lat <= 34
    gz = len(zlib.compress(orjson.dumps(trails), 6))
    assert gz < 3_000_000 and forward["payload"]["gzip_bytes"] == gz
    names = [r["city"] for r in forward["receptors"]]
    assert names == [r.name for r in receptors]
    ludhiana = forward["receptors"][names.index("Ludhiana")]
    assert len(ludhiana["series"]) >= 24
    point = ludhiana["series"][0]
    assert set(point) >= {"t", "smoke_index", "from_fires", "from_scars", "range"}
    assert max(p["smoke_index"] for p in ludhiana["series"]) > 0  # Punjab fires reach Ludhiana
    assert all(p["range"][0] <= p["range"][1] for p in ludhiana["series"])
    assert ludhiana["peak"]["value"] == max(p["smoke_index"] for p in ludhiana["series"])
    assert ludhiana["arrival_t"] <= ludhiana["peak"]["t"]


def test_back_runs_summary(recorded_runs):
    run_at, _, backs, receptors = recorded_runs
    assert [b["city"] for b in backs] == [r.name for r in receptors]
    delhi = backs[0]
    s = delhi["summary"]
    assert set(s) >= {"share_over_burn_belt", "fire_exposure_index", "top_districts", "blh_m_now",
                      "wind_from_deg_now", "cone", "members", "verdict_reason"}
    assert set(s["share_over_burn_belt"]) == {"h24", "h48", "h72"}
    assert s["share_over_burn_belt"]["h24"] is None  # no boundaries loaded in this test
    assert s["fire_exposure_index"] is not None and s["fire_exposure_index"] >= 0
    assert [m["model"] for m in s["members"]] == ["best_match", "gfs_seamless"]
    assert s["cone"] is None or s["cone"]["type"] in ("Polygon", "MultiPolygon")
    assert 0 <= s["wind_from_deg_now"] <= 360
    assert delhi["verdict_hint"] in (None, "stubble_likely", "mixed", "stubble_unlikely")
    assert 0 < len(delhi["trails"]) <= 2 * job.BACK_TRAILS_PER_MEMBER
    trail = delhi["trails"][0]
    assert trail["times"] == sorted(trail["times"])  # oldest first, ending at the city
    assert trail["times"][-1] == pytest.approx(run_at.timestamp(), abs=1)
    ludhiana = backs[[b["city"] for b in backs].index("Ludhiana")]
    assert ludhiana["summary"]["fire_exposure_index"] > 0  # the city sits among the fires


def test_no_fire_data_gives_no_exposure():
    members = recorded_members()
    run_at = datetime.fromtimestamp(float(members[0].wind.t0) + 80 * 3600, UTC)
    backs = job.back_runs(members, recorded_receptors()[:1], None, None, None, None, run_at, seed=1)
    assert backs[0]["summary"]["fire_exposure_index"] is None
    assert backs[0]["verdict_hint"] is None  # nothing to judge from: no fires, no boundaries


# --- storage and routes --------------------------------------------------------------------------


def test_smoke_routes(recorded_runs, monkeypatch):
    from blindspot.api.main import app
    from blindspot.api.routers import smoke as smoke_router

    init_db()
    _, forward, backs, _ = recorded_runs
    monkeypatch.setattr(job, "prune", lambda: {})  # never touch rows other tests own
    smoke_router._cache.clear()
    run_at = datetime.now(UTC)  # newest, so the routes pick these rows
    common = {"met_run_ids": [], "notes": ["test: recorded inputs"],
              "params": {"method": job.method_params(), "sources": [], "members": ["best_match", "gfs_seamless"]}}
    ids = job.store(forward, backs, common, run_at)
    created = [ids["forward"], *ids["back"].values()]
    try:
        with TestClient(app) as client:
            r = client.get("/api/smoke/forward")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["generated_at"].endswith("Z") and isinstance(body["sources"], list)
            assert set(body) >= {"run_at", "t0", "hours", "step_minutes", "inputs", "trails", "receptors"}
            assert body["step_minutes"] == 20 and len(body["trails"]) == len(forward["trails"])
            assert body["inputs"]["fires"] == forward["inputs"]["fires"]
            assert r.headers.get("content-encoding") == "gzip"

            r = client.get("/api/smoke/back", params={"city": "delhi", "hours": 24})
            assert r.status_code == 200, r.text
            back = r.json()
            assert back["city"] == "Delhi" and back["hours"] == 24
            assert set(back["summary"]) >= {"share_over_burn_belt", "fire_exposure_index", "top_districts",
                                            "blh_m_now", "wind_from_deg_now"}
            assert back["verdict_hint"] in (None, "stubble_likely", "mixed", "stubble_unlikely")
            cutoff = datetime.fromisoformat(back["run_at"].replace("Z", "+00:00")).timestamp() - 24 * 3600
            assert all(t >= cutoff - 1 for trail in back["trails"] for t in trail["times"])

            assert client.get("/api/smoke/back", params={"city": "Atlantis"}).status_code == 404
            r = client.get("/api/smoke/receptors")
            assert r.status_code == 200 and {x["city"] for x in r.json()["receptors"]} >= {"Delhi", "Ludhiana"}
            r = client.get("/api/smoke/validation")
            assert r.status_code == 200 and "generated_at" in r.json()
    finally:
        with session_scope() as session:
            session.execute(delete(SmokeRun).where(SmokeRun.id.in_(created)))
        smoke_router._cache.clear()


def test_load_scars_reads_only_candidate_unseen_burn_clusters():
    """Reads the sentinel module's real burn_cluster schema: an unseen scar is loaded (area-weighted
    dry matter, its burn window kept), a FIRMS-matched one and an unmatched (NULL) one are not.
    Rows live only inside a rolled-back transaction, so the shared table is never touched."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from blindspot.db import engine

    init_db()
    if not job._table_exists(Session(bind=engine), "burn_cluster"):
        pytest.skip("burn_cluster table is not created (sentinel module not installed)")
    after, before = datetime(2026, 10, 25, 5, 40, tzinfo=UTC), datetime(2026, 10, 30, 5, 40, tzinfo=UTC)
    insert = text(
        """
        INSERT INTO burn_cluster (geom, centroid, area_ha, n_px, res_m, mgrs_tile, scope, block,
            burn_after, burn_before, gap_days, scene_pre, scene_post, collection, hazy, confidence,
            unseen, model_version)
        VALUES (ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 0.002),
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :area, 10, 20, 'T43REP', 'test', 'test',
                :after, :before, 5, 'test-pre', 'test-post', 'test', false, 0.9, :unseen, 'test')
        """
    )
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            for lon, unseen in ((75.84, True), (75.90, False), (75.96, None)):
                conn.execute(insert, {"lon": lon, "lat": 30.25, "area": 4.0, "after": after,
                                      "before": before, "unseen": unseen})
            scars, note = job.load_scars(Session(bind=conn), after, before + timedelta(hours=1),
                                         (75.8, 30.2, 76.0, 30.3))
        finally:
            tx.rollback()
    assert scars is not None and len(scars) == 1, note
    assert scars.lon[0] == pytest.approx(75.84)
    assert scars.dm[0] == pytest.approx(float(tj.scar_dry_matter_t(np.array([4.0]))[0]))
    hours, shares = tj.scar_release_hours(scars.after[0], scars.before[0], after.timestamp(), before.timestamp())
    assert hours.size and shares.sum() == pytest.approx(1.0)
