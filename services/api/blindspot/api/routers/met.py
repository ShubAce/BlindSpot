"""Weather endpoints, read from met_run / met_grid (the met job fills them; routes never call
Open-Meteo).

- GET /api/met/now?city=Delhi (or lat & lon; optional t): the weather that matters for smoke at a
  place: 10 m and 925 hPa wind, boundary-layer height, 2 m temperature, inversion, last-24 h rain,
  plus the boundary-layer transport wind and the second ensemble member's numbers.
- GET /api/met/wind-field?t=now&level=925hPa: one level of the wind grid at one hour, for the map's
  streamlines. Flat row-major arrays from the south-west corner: index = j * nx + i.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from blindspot.analytics.trajectory import bilinear, wind_speed_from
from blindspot.api import SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.models.met import LEVELS_STORED, PRIMARY_MODEL, MetGrid, MetRun, latest_runs, row_at
from blindspot.provenance import SourceRef, ref_for_fetch, utcnow

router = APIRouter(prefix="/api/met", tags=["met"])

ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"


def _parse_time(value: str | None) -> datetime:
    """`now`/empty -> the current hour; else an ISO-8601 time (naive means UTC)."""
    if value is None or value.strip().lower() in ("", "now"):
        now = utcnow()
        return now.replace(minute=0, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "t must be 'now' or an ISO-8601 time such as 2026-10-30T12:00Z") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _refs(session: Session, runs: list[MetRun]) -> list[SourceRef]:
    refs = []
    for run in runs:
        ref = ref_for_fetch(session, run.fetch_id)
        if ref is not None:
            refs.append(ref)
    return refs


def resolve_place(session: Session, city: str) -> dict[str, Any] | None:
    """Coordinates of a named place: the place table (city, then town), else a receptor the smoke
    job has used (its coordinates came from GeoNames). Database only."""
    row = session.execute(
        text(
            """
            SELECT name, lat, lon, district, state FROM place
            WHERE kind IN ('city', 'town') AND lower(name) = lower(:name)
            ORDER BY (kind = 'city') DESC, population DESC NULLS LAST LIMIT 1
            """
        ),
        {"name": city.strip()},
    ).mappings().first()
    if row is not None:
        return dict(row)
    row = session.execute(
        text(
            """
            SELECT r->>'city' AS name, (r->>'lat')::float8 AS lat, (r->>'lon')::float8 AS lon,
                   r->>'district' AS district, r->>'state' AS state
            FROM smoke_run, jsonb_array_elements(receptors) r
            WHERE kind = 'back' AND lower(city) = lower(:name)
            ORDER BY run_at DESC LIMIT 1
            """
        ),
        {"name": city.strip()},
    ).mappings().first()
    return dict(row) if row is not None else None


def _wind(grid: dict[str, Any], row: MetGrid | None, lon: float, lat: float) -> dict[str, Any] | None:
    if row is None:
        return None
    u = bilinear(grid, row.u, lon, lat)
    v = bilinear(grid, row.v, lon, lat)
    if u is None or v is None:
        return None
    speed, from_deg = wind_speed_from(u, v)
    return {"speed_kmh": round(speed * 3.6, 1), "from_deg": round(from_deg), "u_ms": round(u, 2), "v_ms": round(v, 2)}


def weather_at(
    session: Session, lon: float, lat: float, t: datetime
) -> tuple[dict[str, Any] | None, list[MetRun]]:
    """Everything /api/met/now reports, at one point and hour, plus the met runs it read (for
    SourceRefs). (None, []) before any met run. Other modules can call it too (the agent's
    get_weather, the weather at a burn)."""
    runs = latest_runs(session)
    primary = runs.get(PRIMARY_MODEL) or next(iter(runs.values()), None)
    if primary is None:
        return None, []
    t = min(max(t, primary.t_start), primary.t_end)
    grid = primary.grid
    rows = {lvl: row_at(session, primary, lvl, t) for lvl in ("10m", "925hPa", "bl")}
    surface = rows["10m"]
    if surface is None:
        return None, []
    out: dict[str, Any] = {"t": surface.valid_at, "lat": round(lat, 4), "lon": round(lon, 4)}
    out["basis"] = (
        f"bilinear interpolation of the {primary.dlon:g} deg model grid (a point forecast can differ, "
        "e.g. for rain)"
    )
    out["wind_10m"] = _wind(grid, surface, lon, lat)
    out["wind_925"] = _wind(grid, rows["925hPa"], lon, lat)
    out["transport_wind"] = _wind(grid, rows["bl"], lon, lat)
    blh = bilinear(grid, surface.blh, lon, lat) if surface.blh is not None else None
    out["blh_m"] = None if blh is None else round(blh)
    temp = bilinear(grid, surface.t, lon, lat) if surface.t is not None else None
    out["temp_2m_c"] = None if temp is None else round(temp, 1)
    rain = session.execute(
        select(MetGrid.precip).where(
            MetGrid.run_id == primary.id, MetGrid.level == "10m",
            MetGrid.valid_at > surface.valid_at - timedelta(hours=24),
            MetGrid.valid_at <= surface.valid_at,
        )
    ).scalars().all()
    values = [bilinear(grid, p, lon, lat) for p in rain if p is not None]
    out["rain_mm_24h"] = round(sum(v for v in values if v is not None), 1) if len(values) == 24 else None
    transport = out["transport_wind"]
    out["ventilation_m2_s"] = (
        round(blh * transport["speed_kmh"] / 3.6) if blh is not None and transport is not None else None
    )
    # Inversion: compare 925 hPa (~580 m up) with 2 m, both from the same model (GFS), since
    # best_match mixes models between levels (research section 1).
    out["inversion"] = None
    ensemble: dict[str, Any] = {}
    for model, run in runs.items():
        if run.id == primary.id:
            continue
        s2 = row_at(session, run, "10m", surface.valid_at)
        r925 = row_at(session, run, "925hPa", surface.valid_at)
        rbl = row_at(session, run, "bl", surface.valid_at)
        member = {"blh_m": None, "wind_10m": _wind(run.grid, s2, lon, lat),
                  "transport_wind": _wind(run.grid, rbl, lon, lat), "run_at": run.run_at}
        if s2 is not None and s2.blh is not None:
            value = bilinear(run.grid, s2.blh, lon, lat)
            member["blh_m"] = None if value is None else round(value)
        if s2 is not None and r925 is not None and s2.t is not None and r925.t is not None:
            t2 = bilinear(run.grid, s2.t, lon, lat)
            t925 = bilinear(run.grid, r925.t, lon, lat)
            if t2 is not None and t925 is not None and math.isfinite(t925):
                out["inversion"] = {
                    "present": t925 > t2, "t925_minus_t2m_c": round(t925 - t2, 1), "model": model,
                    "basis": "925 hPa (about 580 m above the plains) minus 2 m temperature, same model",
                }
        ensemble[model] = member
    out["ensemble"] = ensemble
    out["model"] = primary.model
    out["run_at"] = primary.run_at
    out["upstream_runs"] = primary.model_runs
    return out, list(runs.values())


@router.get("/now")
@cached_route(ttl=60)
def met_now(
    session: SessionDep,
    city: str | None = None,
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    t: str | None = None,
):
    """Weather at a city (or a lat/lon) now, or at hour `t`."""
    place = None
    if city:
        place = resolve_place(session, city)
        if place is None and (lat is None or lon is None):
            raise HTTPException(404, f"unknown city {city!r}; pass lat and lon instead")
    if place is None:
        if lat is None or lon is None:
            raise HTTPException(400, "give city=<name>, or lat and lon")
        place = {"name": city, "lat": lat, "lon": lon, "district": None, "state": None}
    data, runs = weather_at(session, float(place["lon"]), float(place["lat"]), _parse_time(t))
    if data is None:
        return respond(
            {"city": place["name"], "t": None, "wind_10m": None, "wind_925": None, "blh_m": None,
             "temp_2m_c": None, "inversion": None, "rain_mm_24h": None,
             "reason": "no meteorology stored yet (the met job has not run)"},
            [], max_age=30,
        )
    if data["wind_10m"] is None:
        data["reason"] = "the point is outside the wind grid"
    payload = {"city": place["name"], "district": place.get("district"), "state": place.get("state"),
               **data, "attribution": ATTRIBUTION}
    return respond(payload, _refs(session, runs), max_age=120)


@router.get("/wind-field")
@cached_route(ttl=120)
def wind_field(
    session: SessionDep,
    t: str | None = "now",
    level: str = "925hPa",
    model: str = PRIMARY_MODEL,
):
    """One level of the wind grid at the hour nearest `t` (u, v in m/s)."""
    if level not in LEVELS_STORED:
        raise HTTPException(400, f"level must be one of {list(LEVELS_STORED)}")
    run = latest_runs(session, [model]).get(model)
    if run is None:
        known = session.scalars(select(func.distinct(MetRun.model))).all()
        if known:
            raise HTTPException(404, f"no stored run of model {model!r}; stored: {sorted(known)}")
        return respond({"t": None, "level": level, "grid": None, "u": [], "v": [],
                        "reason": "no meteorology stored yet (the met job has not run)"}, [], max_age=30)
    when = _parse_time(t)
    row = row_at(session, run, level, min(max(when, run.t_start), run.t_end))
    if row is None:
        return respond({"t": None, "level": level, "grid": run.grid, "u": [], "v": [],
                        "reason": f"run {run.id} has no {level} rows"}, _refs(session, [run]), max_age=30)
    u = np.round(np.asarray(row.u, dtype=np.float64), 2)
    v = np.round(np.asarray(row.v, dtype=np.float64), 2)
    payload = {
        "t": row.valid_at,
        "level": level,
        "grid": run.grid,
        "u": u,
        "v": v,
        "units": "m/s",
        "order": "row-major from the south-west corner: index = j * nx + i, lat = lat0 + j * dlat",
        "model": run.model,
        "run_at": run.run_at,
        "available": {"from": run.t_start, "to": run.t_end, "step_s": run.step_s,
                      "levels": list(LEVELS_STORED)},
        "requested_t": when,
        "attribution": ATTRIBUTION,
    }
    return respond(payload, _refs(session, [run]), max_age=300 if t not in (None, "now") else 120)
