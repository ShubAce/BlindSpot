"""Orbit endpoints: the stored TLEs, satellite passes over a site, the IST look dial, and a live
check of the orbit model against NASA FIRMS. Read PostGIS only; passes are computed from the
stored TLEs (no upstream call).

GET /api/orbits/tle         element sets for the browser (satellite.js), with swath widths
GET /api/orbits/passes      in-swath passes over a site; who is watching now; next and last look
GET /api/orbits/clock       daily IST look windows per satellite and the predicted blind span
GET /api/orbits/validation  predicted closest-approach times vs FIRMS acquisition labels
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics import fires_stats
from blindspot.analytics.passes import (
    FIRE_INSTRUMENTS,
    INSTRUMENTS,
    OrbitElements,
    Site,
    current_status,
    default_site,
    firms_label_errors,
    load_orbits,
    look_clock,
    passes_for_site,
    summarize_errors,
)
from blindspot.api import SessionDep, respond
from blindspot.api.responses import ORJSONResponse
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch, utcnow
from blindspot.sources.celestrak import orbit_mean_elements

router = APIRouter(prefix="/api/orbits", tags=["orbits"])

STALE_TLE_DAYS = 7  # SGP4 error grows by roughly a few km per day of element-set age
MSI_NOTE = (
    "Sentinel-2 passes are predictions: MSI images on sunlit descending passes, but whether a "
    "scene was acquired (and is cloud-free) is for the Sentinel-2 catalogue to say."
)

_cache: TTLCache[Any, Any] = TTLCache(maxsize=256, ttl=60)
_slow_cache: TTLCache[Any, Any] = TTLCache(maxsize=64, ttl=600)
_lock = threading.Lock()


def _cached(cache: TTLCache[Any, Any], key: Any, build: Callable[[], Any]) -> Any:
    with _lock:
        if key in cache:
            return cache[key]
    value = build()
    with _lock:
        cache[key] = value
    return value


def _orbits(session: Session, instruments: str | None = None) -> list[OrbitElements]:
    orbits = load_orbits(session)
    if instruments:
        wanted = {i.strip().upper() for i in instruments.split(",") if i.strip()}
        unknown = wanted - set(INSTRUMENTS)
        if unknown:
            raise HTTPException(400, f"unknown instrument(s) {sorted(unknown)}; use {sorted(INSTRUMENTS)}")
        orbits = [o for o in orbits if o.instrument in wanted]
    return orbits


def _tle_refs(session: Session, orbits: list[OrbitElements]) -> list[SourceRef]:
    refs = [ref_for_fetch(session, i) for i in sorted({o.fetch_id for o in orbits if o.fetch_id})]
    found = [r for r in refs if r is not None]
    return found or latest_refs(session, ["celestrak"])


def _age_hours(orbit: OrbitElements) -> float:
    return round((utcnow() - orbit.epoch).total_seconds() / 3600, 1)


def _satellite_info(orbits: list[OrbitElements], heartbeat: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "satellite": o.name,
            "key": o.key,
            "instrument": o.instrument,
            "half_swath_km": INSTRUMENTS[o.instrument].half_swath_km,
            "tle_epoch": o.epoch,
            "tle_age_hours": _age_hours(o),
            "last_fire_data_utc": heartbeat.get(o.name),  # newest FIRMS row (VIIRS/MODIS only)
        }
        for o in orbits
    ]


def _site(session: Session, lat: float | None, lon: float | None) -> tuple[Site, bool]:
    if (lat is None) != (lon is None):
        raise HTTPException(400, "give both lat and lon, or neither (default: Punjab's centroid)")
    if lat is None or lon is None:
        return default_site(session), True
    return Site(round(lat, 5), round(lon, 5), "requested point", "query parameters"), False


def _notes(orbits: list[OrbitElements]) -> list[str]:
    notes = []
    if any(o.instrument == "MSI" for o in orbits):
        notes.append(MSI_NOTE)
    stale = [o.name for o in orbits if _age_hours(o) > STALE_TLE_DAYS * 24]
    if stale:
        notes.append(f"Element sets older than {STALE_TLE_DAYS} days ({', '.join(stale)}): "
                     "predictions may be off by tens of seconds.")  # fmt: skip
    return notes


@router.get("/tle")
def tle(session: SessionDep) -> ORJSONResponse:
    """The stored element sets: verbatim TLE lines (satellite.js twoline2satrec) and the same
    elements as OMM fields (json2satrec), with each instrument's swath for drawing."""
    orbits = load_orbits(session)
    heartbeat = fires_stats.sensor_heartbeat(session)
    fetched = {
        r.norad_id: (r.fetched_at, r.object_name)
        for r in session.execute(text("SELECT norad_id, fetched_at, object_name FROM tle"))
    }
    satellites = []
    for o in orbits:
        fetched_at, object_name = fetched.get(o.norad_id, (None, o.name))
        instrument = INSTRUMENTS[o.instrument]
        satellites.append(
            {
                "key": o.key,
                "name": o.name,
                "norad_id": o.norad_id,
                "instrument": o.instrument,
                "swath_km": instrument.swath_km,
                "half_swath_km": instrument.half_swath_km,
                "line1": o.line1,
                "line2": o.line2,
                "epoch": o.epoch,
                "epoch_age_hours": _age_hours(o),
                "fetched_at": fetched_at,
                "object_name": object_name,
                "omm": orbit_mean_elements(o.line1, o.line2, object_name),
                "last_fire_data_utc": heartbeat.get(o.name),
            }
        )
    payload: dict[str, Any] = {
        "satellites": satellites,
        "swath_sources": {k: v.citation for k, v in INSTRUMENTS.items()},
    }
    if not satellites:
        payload["note"] = "No element sets yet: the tle job has not run successfully."
    return respond(payload, _tle_refs(session, orbits), max_age=300)


@router.get("/passes")
def passes(
    session: SessionDep,
    lat: float | None = Query(None, ge=-90, le=90, description="site latitude; default Punjab"),
    lon: float | None = Query(None, ge=-180, le=180, description="site longitude"),
    hours_back: float = Query(24, ge=0, le=72),
    hours_ahead: float = Query(48, ge=0, le=168),
    instruments: str | None = Query(None, description="comma list of VIIRS, MODIS, MSI"),
) -> ORJSONResponse:
    """In-swath passes over a site between `hours_back` ago and `hours_ahead` from now, who is
    watching it right now, and the next and last fire-sensor looks."""
    site, is_default = _site(session, lat, lon)
    orbits = _orbits(session, instruments)
    now = utcnow()
    anchor = now.replace(second=0, microsecond=0)  # a cache key that changes once a minute
    start, end = anchor - timedelta(hours=hours_back), anchor + timedelta(hours=hours_ahead)
    key = ("passes", site.lat, site.lon, hours_back, hours_ahead, instruments, anchor,
           tuple((o.norad_id, o.epoch) for o in orbits))  # fmt: skip
    found = _cached(_cache, key, lambda: passes_for_site(orbits, site.lat, site.lon, start, end))
    heartbeat = fires_stats.sensor_heartbeat(session)
    payload: dict[str, Any] = {
        "site": {"lat": site.lat, "lon": site.lon, "name": site.name, "basis": site.basis,
                 "default": is_default},  # fmt: skip
        "window": {"from_utc": start, "to_utc": end},
        "passes": [p.as_dict() for p in found],
        "current": current_status(found, now),
        "satellites": _satellite_info(orbits, heartbeat),
        "visibility_rule": {name: {"half_swath_km": i.half_swath_km, "source": i.citation}
                            for name, i in INSTRUMENTS.items()},  # fmt: skip
        "notes": _notes(orbits),
    }
    if not orbits:
        payload["note"] = "No element sets yet: the tle job has not run successfully."
    return respond(payload, _tle_refs(session, orbits), max_age=30)


@router.get("/clock")
def clock(
    session: SessionDep,
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    days: int = Query(16, ge=1, le=16, description="16 days = one full repeat cycle"),
) -> ORJSONResponse:
    """The 24 h IST dial for a site over the next `days` days: when each satellite looks, the
    windows when any fire sensor looks, and the longest daily gap with no fire-sensor look."""
    site, is_default = _site(session, lat, lon)
    orbits = load_orbits(session)
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    key = ("clock", site.lat, site.lon, days, now, tuple((o.norad_id, o.epoch) for o in orbits))

    def build() -> dict[str, Any]:
        found = passes_for_site(orbits, site.lat, site.lon, now, now + timedelta(days=days))
        return {"passes_counted": len(found), **look_clock(found)}

    body = _cached(_slow_cache, key, build)
    payload = {
        "site": {"lat": site.lat, "lon": site.lon, "name": site.name, "basis": site.basis,
                 "default": is_default},  # fmt: skip
        "window": {"from_utc": now, "to_utc": now + timedelta(days=days)},
        "days": days,
        **body,
        "notes": _notes(orbits)
        + ["Predicted from orbits alone. /api/fires/hours derives the same windows from "
           "actual FIRMS detection times."],  # fmt: skip
    }
    return respond(payload, _tle_refs(session, orbits), max_age=600)


@router.get("/validation")
def validation(
    session: SessionDep,
    days: int = Query(3, ge=1, le=10, description="detections from the last N days"),
    region: Literal["all", "punjab", "haryana", "delhi", "chandigarh"] = "all",
) -> ORJSONResponse:
    """How well the orbit model predicts NASA's own timestamps: for every FIRMS detection in the
    region over the last `days` days, our predicted closest approach of that satellite minus the
    FIRMS acquisition label, in minutes. FIRMS labels are whole minutes in ~2 minute steps."""
    orbits = {o.name: o for o in load_orbits(session) if o.instrument in FIRE_INSTRUMENTS}
    now = utcnow()
    key = ("validation", days, region, now.replace(minute=0, second=0, microsecond=0),
           tuple((o.norad_id, o.epoch) for o in orbits.values()))  # fmt: skip

    def build() -> tuple[dict[str, Any], list[SourceRef]]:
        rf = fires_stats.region_filter(session, region)
        t0 = now - timedelta(days=days)
        rows = session.execute(
            text(
                f"SELECT f.satellite, f.acq_utc, f.lat, f.lon FROM fire_detection f "
                f"WHERE f.acq_utc >= :t0 AND f.acq_utc <= :t1 AND {rf.sql}"
            ),
            {"t0": t0, "t1": now, **rf.params},
        ).all()
        by_sat: dict[str, list[tuple[Any, float, float]]] = defaultdict(list)
        for satellite, acq, lat, lon in rows:
            by_sat[satellite].append((acq, lat, lon))
        results, every_error = [], []
        for name, detections in sorted(by_sat.items()):
            orbit = orbits.get(name)
            if orbit is None:
                results.append({"satellite": name, "detections": len(detections),
                                "note": "no element set stored for this satellite"})  # fmt: skip
                continue
            checks = firms_label_errors(orbit, detections)
            errors = [c.error_min for c in checks if c.error_min is not None]
            every_error += errors
            cross = sorted(c.cross_track_km for c in checks if c.cross_track_km is not None)
            results.append(
                {
                    "satellite": name,
                    "instrument": orbit.instrument,
                    "detections": len(detections),
                    "labels": len({d[0] for d in detections}),
                    "unmatched": sum(1 for c in checks if c.error_min is None),
                    "error_minutes": summarize_errors(errors),
                    "cross_track_km_median": round(cross[len(cross) // 2], 1) if cross else None,
                    "tle_epoch": orbit.epoch,
                }
            )
        payload = {
            "days": days,
            "region": region,
            "window": {"from_utc": t0, "to_utc": now},
            "method": "Predicted time of closest approach of the labelled satellite over each "
            "detection (SGP4 from the stored TLE), minus the FIRMS acquisition time. Positive "
            "means our prediction is later than the label.",
            "overall": summarize_errors(every_error),
            "satellites": results,
            "note": rf.note,
        }
        where = f"f.acq_utc >= :t0 AND f.acq_utc <= :t1 AND {rf.sql}"
        refs = fires_stats.fire_refs(session, where, {"t0": t0, "t1": now, **rf.params})
        return payload, refs + _tle_refs(session, list(orbits.values()))

    payload, sources = _cached(_slow_cache, key, build)
    return respond(payload, sources, max_age=600)
