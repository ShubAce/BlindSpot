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


def test_validation_window_scores_only_spun_up_hours():
    run_at = datetime(2026, 11, 22, 11, 17, 40, tzinfo=UTC)  # the daily 11:17 UTC slot
    day = 86400.0
    # a met run fetched that morning: 9 past days (jobs/met.py PAST_DAYS) from 00:00 UTC, 5 ahead
    start = datetime(2026, 11, 13, tzinfo=UTC).timestamp()
    t0, t_score, t_end = job.validation_window(run_at, start, start + 14 * day - 3600)
    assert t_end == datetime(2026, 11, 22, 11, tzinfo=UTC).timestamp()
    assert t_score == datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp()  # the whole last week...
    assert (t_end - t_score) / 3600 + 1 == 168
    assert t_score - t0 == 48 * 3600 and t0 >= start  # ...with 48 h of hindcast before it
    # with 7 past days of wind (the old PAST_DAYS) the scored window shrinks, the spin-up does not
    start = datetime(2026, 11, 15, tzinfo=UTC).timestamp()
    t0, t_score, t_end = job.validation_window(run_at, start, start + 12 * day - 3600)
    assert t0 == start and t_score == start + 48 * 3600
    assert job.validation_window(run_at, run_at.timestamp() - 40 * 3600, run_at.timestamp() + day) is None


def recorded_validation_inputs(cities: tuple[str, ...] = ("Delhi", "Ludhiana")) -> job.ValidationInputs:
    """Validation inputs from the recorded grids (28-31 Oct) and FIRMS file (29-30 Oct): hindcast
    from the first hour of wind, scored from 48 h later. The PM2.5 'observations' are synthetic (a
    daily cycle plus noise): these tests check the bookkeeping; the statistics have their own."""
    members = recorded_members()
    run_at = datetime(2026, 10, 31, 18, 10, tzinfo=UTC)
    t0, t_score, t_end = job.validation_window(run_at, max(m.wind.t0 for m in members),
                                               min(m.wind.t_end for m in members))
    wind, times, blh = wind_for("best_match")
    hours = np.arange(t_score, t_end + 1.0, 3600.0).tolist()
    rng = np.random.default_rng(1)
    receptors = [r for r in recorded_receptors() if r.name in cities]
    obs = {}
    for r in receptors:
        values = 120.0 + 40.0 * np.sin(2 * np.pi * np.array(hours) / 86400.0) + rng.normal(0, 10.0, len(hours))
        obs[r.name] = {"obs": dict(zip(hours, values.tolist(), strict=True)),
                       "n_stations": dict.fromkeys(hours, 2), "stations": 2,
                       "cams": dict(zip(hours, (0.8 * values).tolist(), strict=True))}
    return job.ValidationInputs(run_at, members, receptors, obs, recorded_fires(), None, t0, t_score, t_end,
                                times, blh, times, wind.u, wind.v)


def test_validation_hindcast_spins_up_before_the_scored_hours():
    inp = recorded_validation_inputs()
    assert inp.t0 == inp.members[0].wind.t0 and inp.t_score - inp.t0 == job.VALIDATION_SPINUP_H * 3600
    p, member_series = job.validation_hindcast(inp, seed=3)
    assert p.t_rel.min() >= inp.t0 and len(p.sources) == len(inp.fires)  # 29 Oct fires are released
    for _, hours, total, _ in member_series:
        assert hours[0] == inp.t_score and hours[-1] == inp.t_end  # only spun-up hours are scored
        assert total.shape == (len(inp.cities), hours.size)
    summary, series = job.validation_scores(inp, p, member_series)
    period = summary["period"]
    assert period["hindcast_from"].timestamp() == inp.t0 and period["from"].timestamp() == inp.t_score
    assert period["to"].timestamp() == inp.t_end and period["spinup_h"] == 48
    assert {s["city"] for s in series} == {"Delhi", "Ludhiana"}
    for s in series:
        assert [pt["t"].timestamp() for pt in s["series"]] == hours.tolist()
    ludhiana = next(s for s in series if s["city"] == "Ludhiana")
    assert ludhiana["series"][0]["smoke_index"] > 0  # the smoke of the spin-up fires has arrived
    assert summary["cities"][0]["city"] == "Delhi" and summary["cities"][0]["hours_observed"] == hours.size


def test_allocate_by_mass_gives_particles_of_equal_weight():
    counts = job.allocate_by_mass(np.array([100.0, 10.0, 1.0, 0.1, 0.0]), budget=50, floor=1)
    assert counts.tolist() == [43, 4, 1, 1, 1] and counts.sum() == 50
    # above the floor every particle carries about the same tonnes (100/43 and 10/4)
    assert 100.0 / counts[0] == pytest.approx(10.0 / counts[1], rel=0.1)
    assert job.allocate_by_mass(np.array([1.0, 2.0, 3.0]), budget=2, floor=1).tolist() == [1, 1, 1]
    rng = np.random.default_rng(0)
    tonnes = rng.lognormal(1.0, 2.0, 2000)
    many = job.allocate_by_mass(tonnes, budget=24_000, floor=1)
    assert many.sum() == 24_000 and many.min() == 1
    big = many >= 10  # whole particles: within half a particle of the exact share
    weight = tonnes[big] / many[big]
    assert big.sum() > 100 and np.all(np.abs(weight / np.median(weight) - 1.0) < 0.1)


def test_validation_particles_follow_the_tonnes_and_seed_is_fixed_by_window():
    fires = recorded_fires()
    lo, hi = float(fires.t.min()), float(fires.t.max())
    p = job.build_forward_particles(fires, None, lo, hi, np.random.default_rng(1), allocation="mass",
                                    budget=job.VALIDATION_PARTICLES, floor=job.VALIDATION_MIN_PER_SOURCE)
    assert p.lat.size == job.VALIDATION_PARTICLES
    per_source = np.bincount(p.source, weights=p.weight, minlength=len(p.sources))
    assert per_source == pytest.approx(np.array([s["dm_t"] for s in p.sources]))  # no smoke lost
    biggest = int(np.argmax(fires.dm))
    assert np.count_nonzero(p.source == biggest) > np.count_nonzero(p.source == int(np.argmin(fires.dm)))
    with pytest.raises(ValueError):
        job.build_forward_particles(fires, None, lo, hi, np.random.default_rng(1), allocation="by_area")
    # the forward run's trails keep an equal number per source
    eq = job.build_forward_particles(fires, None, lo, hi, np.random.default_rng(1))
    assert len(set(np.bincount(eq.source).tolist())) == 1
    # the validation is deterministic: the seed comes from the window, not the clock
    inp = recorded_validation_inputs(("Delhi",))
    a, sa = job.run_validation(inp)
    b, sb = job.run_validation(inp)
    assert a["monte_carlo"]["seed"] == job.validation_seed(inp.t0, inp.t_end) == b["monte_carlo"]["seed"]
    assert sa == sb and a["cities"] == b["cities"]
    assert job.validation_seed(inp.t0, inp.t_end) != job.validation_seed(inp.t0 + 3600, inp.t_end + 3600)


def test_validation_headline_names_its_city():
    """With no measurements near Delhi the headline is another city's result, and says so; the
    method page used to print every headline as Delhi's."""
    inp = recorded_validation_inputs(("Ludhiana", "Chandigarh"))
    first = min(inp.obs["Ludhiana"]["obs"])
    del inp.obs["Ludhiana"]["obs"][first]  # Chandigarh has one hour more
    summary, _ = job.run_validation(inp, seed=3)
    assert [c["city"] for c in summary["cities"]] == ["Chandigarh", "Ludhiana"]
    assert summary["headline_city"] == "Chandigarh"
    assert summary["headline"] == f"Chandigarh: {summary['cities'][0]['summary']}"
    assert "Delhi" not in summary["headline"]
    with_delhi, _ = job.run_validation(recorded_validation_inputs(("Ludhiana", "Delhi")), seed=3)
    assert with_delhi["headline_city"] == "Delhi" and with_delhi["headline"].startswith("Delhi: ")


def test_partial_r_controls_each_series_at_its_own_hour():
    """Observations driven only by their own hour's ventilation leave a model nothing to explain:
    partial r ~ 0 at every lag. Residualising the observation at t + L on the ventilation of the
    model hour t instead (the old alignment) credits a model that merely follows ventilation 3 h
    ahead with a partial r near 1 at lag 3."""
    rng = np.random.default_rng(3)
    n = 480
    hours = np.arange(n)
    log_vent = np.sin(2 * np.pi * hours / 24.0) + np.convolve(rng.normal(0, 0.6, n + 23), np.ones(24) / 24**0.5, "valid")
    obs = 150.0 - 40.0 * log_vent + rng.normal(0, 4.0, n)  # its own hour's ventilation, plus noise
    model = -np.r_[log_vent[3:], np.full(3, np.nan)]  # model(t) = -log_vent(t + 3)
    res = tj.lagged_correlations(model, obs, control=log_vent)
    assert res["by_lag"][3]["r"] > 0.9  # it tracks the observations 3 h ahead...
    assert all(e["partial_r"] is not None and abs(e["partial_r"]) < 0.15 for e in res["by_lag"])  # ...and adds nothing
    ok = np.isfinite(model[:-3])
    old = np.corrcoef(tj._residual(model[:-3][ok], log_vent[:-3][ok]), tj._residual(obs[3:][ok], log_vent[:-3][ok]))[0, 1]
    assert old > 0.8  # what the old alignment reported at lag 3


def test_lag_scan_runs_both_ways():
    """A model that trails the measurements (too slow) shows up at a negative lag."""
    hours = np.arange(200, dtype=float)
    model = np.sin(hours / 9.0) + 1.5
    obs = np.r_[model[5:], np.full(5, np.nan)] * 40.0 + 20.0  # obs(t) = model(t + 5): model 5 h late
    res = tj.lagged_correlations(model, obs, min_lag=-12, max_lag=12)
    assert [e["lag_h"] for e in res["by_lag"]] == list(range(-12, 13))
    assert res["lag0"]["lag_h"] == 0 and res["lag0"]["n_eff"] is not None
    assert res["best_lag"]["lag_h"] == -5 and res["best_lag"]["r"] == pytest.approx(1.0, abs=1e-6)
    assert tj.lagged_correlations(model, obs)["best_lag"]["lag_h"] >= 0  # the old one-sided scan


def test_day_shift_placebo_is_paired_and_needs_day_to_day_signal():
    rng = np.random.default_rng(7)
    n = 168
    hours = np.arange(n)
    blh = 600.0 + 500.0 * np.sin(2 * np.pi * (hours - 6) / 24.0)  # a daily cycle, 100..1100 m
    episodes = np.repeat(rng.lognormal(0.0, 1.0, n // 24), 24)  # smoke that differs day to day
    smoke = episodes * (1.0 + 0.5 * np.sin(2 * np.pi * hours / 24.0))
    obs = 30.0 + 50.0 * smoke / np.maximum(blh, 100.0) * 100.0 + rng.normal(0, 5.0, n)
    res = tj.day_shift_placebo(smoke, blh, obs)
    assert [e["shift_h"] for e in res["by_shift"]] == [-72, -48, -24, 24, 48, 72]
    assert [e["n"] for e in res["by_shift"]] == [n - 72, n - 48, n - 24, n - 24, n - 48, n - 72]
    assert res["model_higher"] == res["compared"] == 6  # the true timing beats every shifted copy
    assert all(e["r_model_same_hours"] > 0.9 for e in res["by_shift"])
    # smoke that is the same every day: moving it by whole days changes nothing, so no skill shows
    same = tj.day_shift_placebo(np.tile(smoke[:24], n // 24), blh, obs)
    assert same["model_higher"] == 0
    assert all(e["r"] == e["r_model_same_hours"] for e in same["by_shift"])
    words = tj.describe_placebos(0.3, 0.4, 0.1)
    assert "higher than the same smoke moved" in words and "not higher than 1/BLH alone" in words


def test_validation_reports_placebos_and_lags_both_ways():
    inp = recorded_validation_inputs(("Delhi", "Ludhiana"))
    summary, series = job.run_validation(inp, seed=3)
    delhi = summary["cities"][0]
    n_hours = len(series[0]["series"])
    model = delhi["smoke_per_km_mixing_depth"]
    assert [e["lag_h"] for e in model["by_lag"]] == list(range(-12, 13)) and model["lag0"]["lag_h"] == 0
    const = delhi["placebos"]["constant_smoke"]
    # the constant-smoke placebo is the correlation of 1/max(BLH, 100 m) with the measurements
    blh = np.interp([p["t"].timestamp() for p in series[0]["series"]], inp.blh_times,
                    tj.point_series(inp.members[0].run.grid, inp.blh, delhi["lon"], delhi["lat"]))
    y = np.array([p["pm25_obs"] for p in series[0]["series"]], dtype=float)
    assert const["lag0"]["r"] == pytest.approx(np.corrcoef(1.0 / np.maximum(blh, 100.0), y)[0, 1], abs=2e-3)
    shift = delhi["placebos"]["day_shift"]  # 43 scored hours here: only the 1-day shifts fit
    assert [e["n"] for e in shift["by_shift"]] == [max(0, n_hours - abs(s)) for s in shift["shifts_h"]]
    ludhiana = summary["cities"][1]["placebos"]["day_shift"]  # Delhi's smoke arrives too late to shift
    assert ludhiana["compared"] == 2 and ludhiana["mean_r"] is not None
    assert summary["median_r_placebo_constant_smoke"] is not None
    assert summary["placebo_comparison"].startswith("Median across cities: smoke model r = ")
    assert summary["headline_placebos"] == delhi["placebo_summary"]


def test_obs_hour_labels_each_period_by_its_centre():
    """A model hour h is the mean of h-30 min..h+30 min. CPCB hourly means arrive stamped at the
    end of their period (:30 UTC, the recorded OpenAQ week), so each belongs to the hour it is
    centred on, not to the next one; 15-minute readings to the hour whose window holds their
    centre; CAMS steps are instantaneous and stay at their own time."""
    from blindspot.sources import openaq

    payload = orjson.loads((FIX.parent / "air" / "openaq_hourly_50.json").read_bytes())
    readings = openaq.parse_hourly(payload, location_id=50, sensor_id=12234796, parameter="pm25")
    starts = {
        datetime.fromisoformat(x["period"]["datetimeTo"]["utc"].replace("Z", "+00:00")):
        datetime.fromisoformat(x["period"]["datetimeFrom"]["utc"].replace("Z", "+00:00"))
        for x in payload["results"]
    }
    assert readings
    for r in readings:
        label = job.obs_hour(r.observed_at.timestamp(), r.period_minutes, False)
        centre = starts[r.observed_at] + (r.observed_at - starts[r.observed_at]) / 2
        assert label == centre.timestamp() and label % 3600 == 0
        assert label == r.observed_at.timestamp() - 1800  # the old ceil() said +1800
    h = datetime(2026, 10, 29, 6, tzinfo=UTC).timestamp()
    ends = [h + m * 60 for m in (15, 30, 45, 60)]  # 15-minute readings ending 06:15..07:00Z
    assert [job.obs_hour(t, 15, False) for t in ends] == [h, h, h + 3600, h + 3600]
    assert job.obs_hour(h, 60, True) == h  # CAMS


def test_load_pm25_puts_rows_on_their_centre_hour():
    """load_pm25 on real air_obs rows (inside a rolled-back transaction): an hourly station, a
    station with 15-minute readings only, and CAMS."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from blindspot.db import engine

    init_db()
    lat, lon = 33.61, 71.37  # far from every other test row; the window is in 2031 as well
    h = datetime(2031, 1, 5, 6, tzinfo=UTC)
    rows = [  # provider, station, observed_at (end of period), period, value, is_model
        ("openaq", "A", h + timedelta(minutes=30), 60, 100.0, False),
        ("openaq", "A", h + timedelta(minutes=45), 15, 999.0, False),  # A has hourly means: skipped
        ("openaq", "B", h + timedelta(minutes=15), 15, 40.0, False),
        ("openaq", "B", h + timedelta(minutes=30), 15, 60.0, False),
        ("openaq", "B", h + timedelta(minutes=45), 15, 80.0, False),
        ("openaq", "B", h + timedelta(minutes=60), 15, 100.0, False),
        ("cams", "Testpur", h, 60, 70.0, True),
        ("cams", "Testpur", h + timedelta(hours=1), 60, 75.0, True),
    ]
    insert = text(
        """
        INSERT INTO air_obs (provider, station_id, station_name, lat, lon, geom, parameter, value,
                             unit, observed_at, period_minutes, is_model)
        VALUES (:p, :s, :s, :lat, :lon, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 'pm25', :v,
                'µg/m³', :t, :m, :model)
        """
    )
    receptor = job.Receptor("Testpur", lat, lon, None, None, "place")
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            for p, s, t, m, v, model in rows:
                conn.execute(insert, {"p": p, "s": s, "lat": lat, "lon": lon, "v": v, "t": t, "m": m,
                                      "model": model})
            out = job.load_pm25(Session(bind=conn), [receptor], h - timedelta(hours=1), h + timedelta(hours=2))
        finally:
            tx.rollback()
    got = out["Testpur"]
    t6, t7 = h.timestamp(), h.timestamp() + 3600
    assert got["obs"] == {t6: pytest.approx((100.0 + 50.0) / 2), t7: pytest.approx(90.0)}
    assert got["n_stations"] == {t6: 2, t7: 1} and got["stations"] == 2
    assert got["cams"] == {t6: 70.0, t7: 75.0}


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
            # Behind a proxy that compresses (GZIP_RESPONSES=false: no gzip middleware, as on the AWS
            # server) the route keeps one plain copy and does not vary on Accept-Encoding, so the
            # cache in front keeps one copy per address.
            from blindspot.api.main import create_app
            from blindspot.config import get_settings

            get_settings().gzip_responses = False
            try:
                with TestClient(create_app()) as proxied:
                    r = proxied.get("/api/smoke/forward")
                    assert r.status_code == 200 and r.json()["step_minutes"] == 20
                    assert "content-encoding" not in r.headers
                    assert "accept-encoding" not in r.headers.get("vary", "").lower()
            finally:
                get_settings().gzip_responses = True

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


def test_load_fires_leaves_out_persistent_heat_sources():
    """A detection inside a fire_static_site zone (a kiln or flare seen hot on 3+ nights) is not
    crop smoke, as in the crop-fire counts; one 5 km away is. Rows live only inside a rolled-back
    transaction."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from blindspot.db import engine

    init_db()
    lat, lon = 33.52, 71.21
    t = datetime(2031, 1, 6, 8, 30, tzinfo=UTC)
    fire = text(
        """
        INSERT INTO fire_detection (source, satellite, instrument, acq_utc, lat, lon, geom,
                                    confidence, frp_mw, daynight)
        VALUES ('VIIRS_NOAA20_NRT', 'NOAA-20', 'VIIRS', :t, :lat, :lon,
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :conf, 6.0, 'D')
        """
    )
    site = text(
        """
        INSERT INTO fire_static_site (area, lat, lon, nights, night_detections, first_night,
                                      last_night, computed_at)
        VALUES (ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 400)::geometry),
                :lat, :lon, 5, 7, '2030-12-01', '2031-01-04', now())
        """
    )
    bbox = (lon - 0.2, lat - 0.2, lon + 0.2, lat + 0.2)
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(site, {"lat": lat, "lon": lon})
            for dlat, conf in ((0.001, "nominal"), (0.045, "nominal"), (0.09, "low")):  # 110 m, 5 km, 10 km
                conn.execute(fire, {"t": t, "lat": lat + dlat, "lon": lon, "conf": conf})
            session = Session(bind=conn)
            fires, note = job.load_fires(session, t - timedelta(hours=1), t + timedelta(hours=1), bbox)
            kept_all, _ = job.load_fires(session, t - timedelta(hours=1), t + timedelta(hours=1), bbox,
                                         exclude_static=False)
        finally:
            tx.rollback()
    assert len(fires) == 1 and fires.lat[0] == pytest.approx(lat + 0.045)
    assert fires.stats["static_site_skipped"] == 1 and fires.stats["low_confidence_skipped"] == 1
    assert "1 at persistent industrial heat sources" in note
    assert len(kept_all) == 2


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
