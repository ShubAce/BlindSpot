"""Geostationary fire watch endpoints (contract additions), read from the GK-2A tables.

- GET /api/geostationary/diurnal?days=3: fire pixels per 10-minute GK-2A scan over Punjab +
  Haryana, the mean per IST hour, the IST times of day when a polar satellite looked (from FIRMS
  detections in fire_detection), the share of fire activity outside those windows, and how well
  GK-2A agrees with VIIRS and MODIS (geo_fire_validation).
- GET /api/geostationary/latest: the newest analysed scan and its fire pixels as GeoJSON
  (pixel footprints, ~12 km2: centres ~4.5 km apart along a row, ~3.6 km down a column).

Counts are fire pixels, an activity index, never a number of fires: one pixel covers ~12 km2 and
may hold one fire or many, and GK-2A misses most small midday fires (see `validation`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from blindspot.analytics import geofire as gf
from blindspot.api import SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.models.geofires import GeoFireSlot, GeoFireValidation
from blindspot.provenance import latest_refs, ref_for_fetch, utcnow
from blindspot.sources import gk2a  # noqa: F401  (registers the gk2a source label)

router = APIRouter(prefix="/api/geostationary", tags=["geostationary"])

SCAN_MINUTES = 10  # GK-2A full-disk repeat cycle: each scan stands for 10 minutes
POLAR_PAD_MIN = (
    15  # a polar look is its detections' time span, padded this much each side
)
REGION_ISOS = ["IN-PB", "IN-HR"]
UNIT = (
    "GK-2A fire pixels (~12 km2 each) per 10-minute scan inside Punjab and Haryana (India's state "
    "polygons): an activity index, not a number of fires"
)
VIIRS = ("Suomi NPP", "NOAA-20", "NOAA-21")


def _polar_passes(
    session: Session, since: datetime, until: datetime
) -> list[gf.PolarPass]:
    """Polar-satellite looks at the region in the period, from FIRMS detections in fire_detection
    clipped to the Punjab and Haryana polygons.

    The diurnal answer uses each pass's satellite, start and end only, so the database returns one
    row per satellite and acquisition time (FIRMS times are to the minute: a pass is 1-3 rows, not
    hundreds of detections). Grouping distinct times gives the same passes, starts and ends;
    n_detections, median_scan_km and frp_sum_mw then describe the distinct times, and nothing in
    this module reads them."""
    rows = session.execute(
        text(
            "SELECT f.acq_utc, f.satellite, min(f.instrument) AS instrument, min(f.scan) AS scan, "
            "sum(f.frp_mw) AS frp_mw "
            "FROM fire_detection f JOIN boundary b ON b.level = 'state' "
            "AND b.props->>'iso3166_2' = ANY(:isos) AND ST_Covers(b.geom, f.geom) "
            "WHERE f.acq_utc BETWEEN :since AND :until GROUP BY f.satellite, f.acq_utc"
        ),
        {"isos": REGION_ISOS, "since": since, "until": until},
    ).all()
    if not rows:
        return []
    return gf.group_passes(
        np.array([r.acq_utc.replace(tzinfo=None) for r in rows], dtype="datetime64[s]"),
        np.array([r.satellite for r in rows], dtype=object),
        np.array([r.instrument for r in rows], dtype=object),
        np.array([np.nan if r.scan is None else r.scan for r in rows], dtype=float),
        np.array([np.nan if r.frp_mw is None else r.frp_mw for r in rows], dtype=float),
    )


def _validation(session: Session) -> dict[str, Any]:
    """Pooled GK-2A vs polar comparisons for the current algorithm (every stored pass).

    Top-level numbers are against VIIRS (all passes, day and night); `groups` splits them by
    satellite family and day/night, because GK-2A's skill differs sharply between them."""
    rows = [
        {c.name: getattr(r, c.name) for c in GeoFireValidation.__table__.columns}
        for r in session.execute(
            select(GeoFireValidation).where(
                GeoFireValidation.algorithm == gf.ALGORITHM_VERSION
            )
        ).scalars()
    ]
    groups = {
        "viirs_all": [r for r in rows if r["satellite"] in VIIRS],
        "viirs_day": [
            r for r in rows if r["satellite"] in VIIRS and r["regime"] == "day"
        ],
        "viirs_night": [
            r for r in rows if r["satellite"] in VIIRS and r["regime"] == "night"
        ],
        "modis_aqua_day": [
            r for r in rows if r["satellite"] == "Aqua" and r["regime"] == "day"
        ],
        "modis_terra": [r for r in rows if r["satellite"] == "Terra"],
        "all": rows,
    }
    pooled = {name: gf.pooled_rates(g) for name, g in groups.items()}
    viirs = pooled["viirs_all"]
    starts = [r["pass_start_utc"] for r in rows]
    return {
        "reference": "VIIRS (S-NPP, NOAA-20, NOAA-21) from FIRMS",
        "hit_rate": viirs["hit_rate"],
        "false_alarm_rate": viirs["false_alarm_rate"],
        "n": viirs["n"],
        "n_geo": viirs["n_geo"],
        "passes": viirs["passes"],
        "groups": pooled,
        "period": [min(starts), max(starts)] if starts else None,
        "algorithm": gf.ALGORITHM_VERSION,
        "method": (
            "Each polar pass is compared with the GK-2A scan nearest in time (within 6 min) on "
            "the GK-2A grid inside Punjab + Haryana. hit_rate = polar fire pixels GK-2A also "
            "flagged (same or adjacent pixel) / polar fire pixels (n); false_alarm_rate = GK-2A "
            "fire pixels with no polar detection nearby / GK-2A fire pixels (n_geo). 'strong' = "
            "polar pixels whose summed FRP >= 30 MW."
        ),
    }


@router.get("/diurnal")
@cached_route(ttl=60)
def diurnal(session: SessionDep, days: Annotated[int, Query(ge=1, le=7)] = 3) -> Any:
    """Fire activity through the day from GK-2A's 10-minute scans, against the polar windows."""
    now = utcnow()
    since = now - timedelta(days=days)
    slots = session.execute(  # the columns used here, not each scan's stats (Neon egress)
        select(GeoFireSlot.status, GeoFireSlot.t_utc, GeoFireSlot.count, GeoFireSlot.count_pb,
               GeoFireSlot.count_hr, GeoFireSlot.clear_frac)
        .where(
            GeoFireSlot.algorithm == gf.ALGORITHM_VERSION, GeoFireSlot.slot_utc >= since
        )
        .order_by(GeoFireSlot.slot_utc)
    ).all()
    ok, failed = [], 0
    for s in slots:
        if s.status == "ok" and s.t_utc is not None and s.count is not None:
            ok.append(s)
        else:
            failed += 1
    sources = latest_refs(session, ["gk2a", "firms"])
    validation = _validation(session)
    passes = _polar_passes(session, since, now)
    windows = gf.polar_windows(passes, POLAR_PAD_MIN)
    payload: dict[str, Any] = {
        "days": days,
        "since": since,
        "unit": UNIT,
        "region": ["Punjab", "Haryana"],
        "scans": len(ok),
        "scans_failed": failed,
        "slots": [
            {
                "t": s.t_utc,
                "count": s.count,
                "count_pb": s.count_pb,
                "count_hr": s.count_hr,
                "clear_frac": s.clear_frac,
            }
            for s in ok
        ],
        "hourly_ist": gf.hourly_means([(s.t_utc, s.count) for s in ok]),
        "polar_windows_ist": windows,
        "polar_passes": len(passes),
        "peak_ist": None,
        "peak_mean_count": None,
        "pixel_hours": None,
        "pixel_hours_outside_polar_windows": None,
        "share_outside_polar_windows": None,
        "unobserved_gaps_ist": [],
        "interpretation": [],
        "validation": validation,
    }
    if not ok:
        payload["reason"] = (
            "no GK-2A scans analysed in this period yet (job geostationary)"
        )
        return respond(payload, sources, max_age=60)
    hourly = [h for h in payload["hourly_ist"] if h["mean_count"] is not None]
    peak = max(hourly, key=lambda h: h["mean_count"])
    if peak["mean_count"] > 0:
        payload["peak_ist"] = f"{peak['hour']:02d}:00-{(peak['hour'] + 1) % 24:02d}:00"
        payload["peak_mean_count"] = peak["mean_count"]
    total = sum(s.count for s in ok)
    payload["pixel_hours"] = round(total * SCAN_MINUTES / 60, 1)
    if not windows:
        payload["reason"] = (
            "no FIRMS detections in fire_detection for this period: polar windows unknown"
        )
    elif total:
        outside = sum(
            s.count
            for s in ok
            if not gf.inside_windows(gf.ist_minute_of_day(s.t_utc), windows)
        )
        payload["pixel_hours_outside_polar_windows"] = round(
            outside * SCAN_MINUTES / 60, 1
        )
        payload["share_outside_polar_windows"] = round(outside / total, 4)
    gaps = _gaps(windows)
    for g in gaps:  # GK-2A activity inside each unobserved span
        lo = int(g["start"][:2]) * 60 + int(g["start"][3:])
        hi = lo + g["minutes"]
        inside = sum(
            s.count
            for s in ok
            if lo <= gf.ist_minute_of_day(s.t_utc) < hi
            or lo <= gf.ist_minute_of_day(s.t_utc) + 1440 < hi
        )
        g["pixel_hours"] = round(inside * SCAN_MINUTES / 60, 1)
    payload["unobserved_gaps_ist"] = gaps
    payload["interpretation"] = _interpretation(payload)
    return respond(payload, sources, max_age=60)


def _gaps(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spans of the IST day (between polar windows, wrapping past midnight) with no polar look,
    longest first."""
    if not windows:
        return []
    out = []
    for i, w in enumerate(windows):
        nxt = windows[(i + 1) % len(windows)]
        start, end = (
            w["end_min"],
            nxt["start_min"] + (1440 if i == len(windows) - 1 else 0),
        )
        if end > start:
            out.append(
                {
                    "start": f"{start % 1440 // 60:02d}:{start % 60:02d}",
                    "end": f"{end % 1440 // 60:02d}:{end % 60:02d}",
                    "minutes": end - start,
                }
            )
    return sorted(out, key=lambda g: -g["minutes"])


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _interpretation(p: dict[str, Any]) -> list[str]:
    """Plain-language statements of what the numbers support, built from the numbers."""
    out = []
    groups = p["validation"]["groups"]
    aqua, vday = groups["modis_aqua_day"], groups["viirs_day"]
    if aqua["passes"]:
        out.append(
            f"Afternoon (MODIS Aqua passes): GK-2A found {_pct(aqua['hit_rate_strong'])} "
            f"of strong fire pixels (n={aqua['n_strong']}) and {_pct(aqua['hit_rate'])} of all "
            f"(n={aqua['n']}); {_pct(aqua['false_alarm_rate'])} of its pixels were unconfirmed "
            f"(n={aqua['n_geo']})."
        )
    if vday["passes"]:
        out.append(
            f"Midday (VIIRS day passes): GK-2A found only {_pct(vday['hit_rate'])} of VIIRS "
            f"fire pixels (n={vday['n']}) and {_pct(vday['false_alarm_rate'])} of its pixels were "
            f"unconfirmed (n={vday['n_geo']}): small midday fires are below its noise, so midday "
            "counts are not reliable."
        )
    if p.get("share_outside_polar_windows") is not None:
        out.append(
            f"{_pct(p['share_outside_polar_windows'])} of GK-2A fire pixel-hours "
            f"({p['pixel_hours_outside_polar_windows']} of {p['pixel_hours']}) fell outside the "
            f"times a polar satellite looked; activity peaked {p['peak_ist']} IST."
        )
    gaps = [g for g in p.get("unobserved_gaps_ist") or [] if g.get("pixel_hours")]
    if gaps:
        g = max(gaps, key=lambda g: g["pixel_hours"])
        out.append(
            f"Most GK-2A activity no polar satellite saw fell in {g['start']}-{g['end']} IST "
            f"({g['pixel_hours']} pixel-hours); counts there "
            "cannot be checked against a polar satellite, so treat them as a relative activity "
            "index (shape of the day), not a fire count."
        )
    return out


@router.get("/latest")
@cached_route(ttl=60)
def latest(
    session: SessionDep,
    min_confidence: Annotated[int, Query(ge=1, le=2)] = 1,
    slot: Annotated[
        datetime | None, Query(description="a scan slot (UTC); default: the newest")
    ] = None,
) -> Any:
    """The newest analysed GK-2A scan (or the one asked for) and its fire pixels as GeoJSON."""
    query = select(GeoFireSlot).where(
        GeoFireSlot.algorithm == gf.ALGORITHM_VERSION, GeoFireSlot.status == "ok"
    )
    if slot is not None:
        query = query.where(GeoFireSlot.slot_utc == slot)
    row = session.execute(
        query.order_by(GeoFireSlot.slot_utc.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        if slot is not None:
            raise HTTPException(404, "no analysed GK-2A scan at that slot")
        return respond(
            {
                "scan": None,
                "unit": UNIT,
                "fires": {"type": "FeatureCollection", "features": []},
                "reason": "no GK-2A scan analysed yet (job geostationary)",
            },
            latest_refs(session, ["gk2a"]),
            max_age=60,
        )
    features = (
        session.execute(
            text(
                "SELECT json_build_object('type', 'Feature', "
                "'geometry', ST_AsGeoJSON(coalesce(footprint, geom), 5)::json, "
                "'properties', json_build_object('lon', lon, 'lat', lat, 'state', state, "
                "'confidence', CASE confidence WHEN 2 THEN 'high' ELSE 'nominal' END, "
                "'frp_mw', frp_mw, 'excess_k', excess_k, 'bt38_k', bt38_k, 'bt11_k', bt11_k, "
                "'area_km2', area_km2, 'absolute_test', absolute)) "
                "FROM geo_fire_pixel WHERE slot_utc = :slot AND algorithm = :alg "
                "AND confidence >= :conf ORDER BY frp_mw DESC NULLS LAST"
            ),
            {"slot": row.slot_utc, "alg": gf.ALGORITHM_VERSION, "conf": min_confidence},
        )
        .scalars()
        .all()
    )
    ref = ref_for_fetch(session, row.fetch_id)
    sources = [ref] if ref is not None else latest_refs(session, ["gk2a"])
    age = (utcnow() - row.t_utc).total_seconds() / 60 if row.t_utc else None
    return respond(
        {
            "scan": {
                "slot_utc": row.slot_utc,
                "t": row.t_utc,
                "age_min": round(age, 1) if age is not None else None,
                "count": row.count,
                "count_pb": row.count_pb,
                "count_hr": row.count_hr,
                "count_high": row.count_high,
                "frp_sum_mw": row.frp_sum_mw,
                "clear_frac": row.clear_frac,
                "sza_deg": row.sza_deg,
                "daylight": row.sza_deg is not None and row.sza_deg < 90,
                "algorithm": row.algorithm,
            },
            "unit": UNIT,
            "fires": {"type": "FeatureCollection", "features": list(features)},
        },
        sources,
        max_age=60,
    )
