"""Job `met`: the meteorology grid that the smoke model and the weather endpoints read.

Every 3 hours, for each ensemble member, Open-Meteo `best_match` and `gfs_seamless` (the only two
models with both pressure-level winds and boundary-layer height over the whole window, research
section 1; the two-model ensemble is a SYNTHESIS decision):
1. Ask Open-Meteo's meta.json whether a new upstream model run has landed since our last fetch.
   Models update every 6 h, so fetching more often would only spend the free-tier budget.
2. If so (or the stored run is old, or the grid changed), fetch the member's whole grid with one
   POST: winds at 10 m / 950 / 925 / 850 hPa, boundary-layer height, surface pressure, 2 m
   temperature, plus precipitation (best_match) or 925 hPa temperature (gfs_seamless, so the
   inversion diagnostic compares two temperatures from the same model). 7 past days + 5 forecast
   days cost the same as fewer days (Open-Meteo counts in 14-day units).
3. Compute each pressure level's height above ground (hypsometric equation) and the
   boundary-layer mean wind, and store everything as a new met_run with its met_grid rows, in one
   transaction. Keep the newest few runs per model.

Budget: 12 variables count 1.2 calls per location, so a 425-point grid is ~510 calls per member,
~1,020 per refresh, at most ~4 refreshes a day: ~4,100 of the free 10,000 calls/day. The two POSTs
are spaced 61 s apart so a minute never sees more than 600 calls.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import delete, select

from blindspot.analytics.trajectory import bl_mean_wind, pressure_level_heights
from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.http import FetchError
from blindspot.jobs import job
from blindspot.models.met import PRIMARY_MODEL, MetRun, latest_runs
from blindspot.provenance import utcnow
from blindspot.sources.openmeteo import (
    LEVELS,
    PRESSURE_LEVELS_HPA,
    WIND_VARIABLES,
    GridSpec,
    ModelGrid,
    fetch_grid,
    fetch_model_meta,
)

log = logging.getLogger(__name__)

SURFACE = ("boundary_layer_height", "surface_pressure", "temperature_2m")
MEMBERS: dict[str, tuple[str, ...]] = {
    "best_match": WIND_VARIABLES + SURFACE + ("precipitation",),
    "gfs_seamless": WIND_VARIABLES + SURFACE + ("temperature_925hPa",),
}
# Which upstream runs feed each member: best_match takes its surface fields from ECMWF IFS 9 km
# and its pressure levels mostly from ICON (research section 1); gfs_seamless is GFS 0.11/0.25 deg.
META_DOMAINS: dict[str, tuple[str, ...]] = {
    "best_match": ("ecmwf_ifs", "dwd_icon"),
    "gfs_seamless": ("ncep_gfs013", "ncep_gfs025"),
}
PAST_DAYS = 7  # back runs need 72 h; the validation hindcast uses the whole week
FORECAST_DAYS = 5  # forward runs need 48 h ahead of any time on the fetch day
MIN_REFRESH = timedelta(hours=5)  # never refetch a member sooner (upstream runs are 6 h apart)
MAX_AGE = timedelta(hours=9)  # always refetch a run older than this, new upstream run or not
STALE_FAIL = timedelta(hours=24)  # fail the job if the primary member is older and cannot refresh
KEEP_RUNS = 3  # stored runs per model
PACE_S = 61.0  # seconds between member POSTs (600 calls/min)


@dataclass
class Decision:
    fetch: bool
    reason: str
    model_runs: dict[str, Any] = field(default_factory=dict)


def _call_weight(spec: GridSpec, n_vars: int, days: int) -> float:
    """Open-Meteo's cost of one request in "calls" (research section 1, call weight)."""
    return spec.n * max(1.0, max(1.0, days / 14) * n_vars / 10)


def _same_grid(run: MetRun, spec: GridSpec) -> bool:
    return (
        run.nx == spec.nx and run.ny == spec.ny
        and abs(run.lon0 - spec.lon0) < 1e-9 and abs(run.lat0 - spec.lat0) < 1e-9
        and abs(run.dlon - spec.dlon) < 1e-9 and abs(run.dlat - spec.dlat) < 1e-9
    )


def _upstream_runs(model: str) -> tuple[dict[str, Any], list[str]]:
    """meta.json of each upstream model behind a member. A failed lookup is noted, not fatal."""
    runs: dict[str, Any] = {}
    errors = []
    for domain in META_DOMAINS.get(model, ()):
        try:
            runs[domain] = fetch_model_meta(domain)
        except FetchError as exc:
            errors.append(f"{domain}: {exc}")
    return runs, errors


def decide(latest: MetRun | None, spec: GridSpec, model: str, now: datetime) -> Decision:
    """Whether to fetch `model` now, and why (the reason ends up in job_run.result).

    The upstream run metadata is looked up whenever a fetch is possible, so every stored run
    records which ECMWF / ICON / GFS runs it came from.
    """
    if latest is not None and _same_grid(latest, spec):
        age = now - latest.run_at
        if age < MIN_REFRESH:
            return Decision(False, f"fetched {age.total_seconds() / 3600:.1f} h ago")
    runs, errors = _upstream_runs(model)
    if latest is None:
        return Decision(True, "no stored run", runs)
    if not _same_grid(latest, spec):
        return Decision(True, "the grid (MET_BBOX) changed", runs)
    age = now - latest.run_at
    if age >= MAX_AGE:
        return Decision(True, f"stored run is {age.total_seconds() / 3600:.1f} h old", runs)
    stored = latest.model_runs or {}
    for domain, info in runs.items():
        before = (stored.get(domain) or {}).get("available")
        if info.get("available") and info["available"] != before:
            return Decision(True, f"new {domain} run initialised {info.get('init')}", runs)
    if errors and not runs:
        return Decision(True, "model-run metadata unavailable: " + "; ".join(errors), runs)
    return Decision(False, "no new upstream model run since the stored fetch", runs)


def _rows(run_id: int, model: str, run_at: datetime, grid: ModelGrid) -> list[dict[str, Any]]:
    """met_grid rows: the four levels plus the boundary-layer mean wind, for every hour."""
    f = grid.fields
    heights = pressure_level_heights(
        f["surface_pressure"], f["temperature_2m"], list(PRESSURE_LEVELS_HPA.values())
    )
    u_levels = [f[f"wind_u_component_{lvl}"] for lvl in LEVELS]
    v_levels = [f[f"wind_v_component_{lvl}"] for lvl in LEVELS]
    u_bl, v_bl = bl_mean_wind(u_levels, v_levels, [10.0, *heights], f["boundary_layer_height"])
    spec = grid.spec
    common = {
        "run_id": run_id, "model": model, "run_at": run_at, "lon0": spec.lon0, "lat0": spec.lat0,
        "dlon": spec.dlon, "dlat": spec.dlat, "nx": spec.nx, "ny": spec.ny,
        "fetch_id": grid.fetch_id,
    }

    def flat(array: np.ndarray, k: int, digits: int = 2) -> list[float]:
        return np.round(array[k].astype(np.float64).ravel(), digits).tolist()

    rows = []
    for k, ts in enumerate(grid.times.tolist()):
        valid_at = datetime.fromtimestamp(ts, UTC)
        for level, u_arr, v_arr in zip(LEVELS, u_levels, v_levels, strict=True):
            row = {**common, "valid_at": valid_at, "level": level,
                   "u": flat(u_arr, k), "v": flat(v_arr, k), "blh": None, "t": None, "precip": None}
            if level == "10m":
                row["blh"] = flat(f["boundary_layer_height"], k, 0)
                row["t"] = flat(f["temperature_2m"], k, 1)
                if "precipitation" in f:
                    row["precip"] = flat(f["precipitation"], k, 1)
            elif level == "925hPa" and "temperature_925hPa" in f:
                row["t"] = flat(f["temperature_925hPa"], k, 1)
            rows.append(row)
        rows.append({**common, "valid_at": valid_at, "level": "bl", "u": flat(u_bl, k, 3),
                     "v": flat(v_bl, k, 3), "blh": None, "t": None, "precip": None})
    return rows


_COPY_COLUMNS = (
    "run_id", "model", "run_at", "valid_at", "level", "lon0", "lat0", "dlon", "dlat", "nx", "ny",
    "u", "v", "blh", "t", "precip", "fetch_id",
)


def _copy_rows(session: Any, rows: list[dict[str, Any]]) -> None:
    """Bulk-load met_grid rows with Postgres COPY, inside the session's transaction.

    ~1,400 rows of 425-element arrays take about a second this way, against ~26 s as an
    executemany INSERT (measured on the dev box)."""
    driver = session.connection().connection.driver_connection  # the psycopg 3 connection
    with driver.cursor() as cur:
        sql = f"COPY met_grid ({', '.join(_COPY_COLUMNS)}) FROM STDIN"
        with cur.copy(sql) as copy:
            for row in rows:
                copy.write_row(tuple(row[c] for c in _COPY_COLUMNS))


def store_grid(model: str, grid: ModelGrid, model_runs: dict[str, Any], run_at: datetime) -> int:
    """Write one fetched member as a met_run plus its met_grid rows, in one transaction."""
    spec = grid.spec
    t0 = time.perf_counter()
    with session_scope() as session:
        run = MetRun(
            model=model,
            run_at=run_at,
            t_start=datetime.fromtimestamp(int(grid.times[0]), UTC),
            t_end=datetime.fromtimestamp(int(grid.times[-1]), UTC),
            n_times=int(grid.times.size),
            step_s=int(grid.times[1] - grid.times[0]),
            lon0=spec.lon0, lat0=spec.lat0, dlon=spec.dlon, dlat=spec.dlat, nx=spec.nx, ny=spec.ny,
            levels=[*LEVELS, "bl"],
            variables=sorted(grid.fields),
            elevation=np.round(grid.elevation.astype(np.float64).ravel(), 0).tolist(),
            model_runs={k: {kk: v.get(kk) for kk in ("init", "available", "fetch_id")}
                        for k, v in model_runs.items()},
            stats=grid.stats,
            fetch_id=grid.fetch_id,
        )
        session.add(run)
        session.flush()
        rows = _rows(run.id, model, run_at, grid)
        _copy_rows(session, rows)
        run.stats = {**grid.stats, "rows": len(rows), "store_ms": round((time.perf_counter() - t0) * 1000)}
        run_id = run.id
    return run_id


def prune(model: str, keep: int = KEEP_RUNS) -> int:
    """Delete all but the newest `keep` runs of `model` (their grid rows go with them)."""
    with session_scope() as session:
        ids = session.scalars(
            select(MetRun.id).where(MetRun.model == model)
            .order_by(MetRun.run_at.desc(), MetRun.id.desc()).offset(keep)
        ).all()
        if ids:
            session.execute(delete(MetRun).where(MetRun.id.in_(ids)))
    return len(ids)


@job("met", every_minutes=180)
def met() -> dict[str, Any]:
    """Open-Meteo multi-level wind + boundary-layer grid (2-model ensemble) -> met_run/met_grid."""
    spec = GridSpec.covering(get_settings().met_bbox)
    now = utcnow()
    with session_scope() as session:
        latest = latest_runs(session, MEMBERS)
        session.expunge_all()
    summary: dict[str, Any] = {"grid": {**spec.as_dict(), "points": spec.n}, "members": {}}
    last_post = 0.0
    primary_error: str | None = None
    for model, variables in MEMBERS.items():
        decision = decide(latest.get(model), spec, model, now)
        entry: dict[str, Any] = {"reason": decision.reason}
        summary["members"][model] = entry
        if not decision.fetch:
            entry["run_id"] = latest[model].id if model in latest else None
            continue
        wait = PACE_S - (time.monotonic() - last_post)
        if last_post and wait > 0:
            time.sleep(wait)
        try:
            grid = fetch_grid(spec, model, variables, past_days=PAST_DAYS, forecast_days=FORECAST_DAYS)
        except (FetchError, ValueError) as exc:  # ValueError: an answer that does not parse
            last_post = time.monotonic()
            entry["error"] = str(exc)[:300]
            log.warning("met: %s fetch failed: %s", model, exc)
            if model == PRIMARY_MODEL:
                primary_error = str(exc)[:300]
            continue
        last_post = time.monotonic()
        grid.stats["call_weight"] = round(_call_weight(spec, len(variables), PAST_DAYS + FORECAST_DAYS))
        run_id = store_grid(model, grid, decision.model_runs, utcnow())
        entry.update(run_id=run_id, hours=int(grid.times.size), fetch_id=grid.fetch_id,
                     latency_ms=grid.stats.get("latency_ms"), call_weight=grid.stats["call_weight"],
                     pruned=prune(model))
    if primary_error is not None:
        previous = latest.get(PRIMARY_MODEL)
        if previous is None or now - previous.run_at > STALE_FAIL:
            raise RuntimeError(f"{PRIMARY_MODEL} grid could not be fetched and no recent run is stored: {primary_error}")
        summary["warning"] = f"{PRIMARY_MODEL} refresh failed; serving the run fetched {previous.run_at:%Y-%m-%d %H:%MZ}"
    return summary
