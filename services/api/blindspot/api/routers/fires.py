"""Fire endpoints: NASA FIRMS detections and the official CREAMS counts. Read PostGIS only.

GET /api/fires            detections in the last N hours, as GeoJSON points, with counts
GET /api/fires/daily      detections per IST day since the season start (15 Sep)
GET /api/fires/hours      detections by IST hour, the windows when satellites reported fires,
                          and the longest daily span with (almost) no reports
GET /api/fires/official   CREAMS (ICAR-IARI) official events per IST day (contract addition)

Counts are clipped to Indian state polygons (Punjab, Haryana, Delhi, Chandigarh for
region=all), never to a lat/lon box: the AOI box also covers Pakistan's Punjab.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from blindspot.analytics import fires_stats
from blindspot.api import BBoxDep, SessionDep, respond
from blindspot.api.cache import get_or_build
from blindspot.api.responses import ORJSONResponse
from blindspot.geo import IST
from blindspot.provenance import utcnow
from blindspot.sources import creams, firms  # noqa: F401  registers source labels in the API process

router = APIRouter(prefix="/api/fires", tags=["fires"])

Region = Literal["all", "punjab", "haryana", "delhi", "chandigarh"]
Confidence = Literal["low", "nominal", "high"]
CONFIDENCE_AT_LEAST = {"low": ["low", "nominal", "high"], "nominal": ["nominal", "high"],
                       "high": ["high"]}  # fmt: skip
CACHE_S = 30


def _cached(key: Any, build: Callable[[], Any]) -> Any:
    """Serve identical requests within 30 s from memory (architecture.md section 8), through the
    shared single-flight cache so simultaneous identical requests query the database once."""
    return get_or_build("fires", key, build, ttl=CACHE_S, maxsize=256)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.get("")
def fires(
    session: SessionDep,
    bbox: BBoxDep,
    hours: int = Query(24, ge=1, le=720, description="window length, hours before `end`"),
    min_confidence: Confidence | None = Query(None, description="keep this class and above"),
    region: Region = Query("all", description="Indian state(s) to keep (clip=india)"),
    clip: Literal["india", "none"] = Query(
        "india", description="india: only detections inside the region's state polygons; "
        "none: everything in the bbox, including neighbouring countries"),
    end: datetime | None = Query(None, description="window end (ISO-8601, UTC); default now"),
    limit: int = Query(20000, ge=1, le=50000, description="most features returned, newest first"),
) -> ORJSONResponse:  # fmt: skip
    """FIRMS detections as GeoJSON points (newest first), with the count per satellite."""
    t1 = _as_utc(end)
    t0 = t1 - timedelta(hours=hours)
    key = ("fires", hours, bbox, min_confidence, region, clip, end and t1, limit)

    def build() -> tuple[dict[str, Any], list[Any]]:
        params: dict[str, Any] = {"t0": t0, "t1": t1, "x0": bbox[0], "y0": bbox[1],
                                  "x1": bbox[2], "y1": bbox[3]}  # fmt: skip
        where = [
            "f.acq_utc > :t0",
            "f.acq_utc <= :t1",
            "ST_Intersects(f.geom, ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326))",
        ]
        note = None
        if min_confidence:
            where.append("f.confidence = ANY(:levels)")
            params["levels"] = CONFIDENCE_AT_LEAST[min_confidence]
        if clip == "india":
            rf = fires_stats.region_filter(session, region)
            where.append(rf.sql)
            params.update(rf.params)
            note = rf.note
        else:
            note = "Not clipped: includes detections outside India inside the bbox."
        sql_where = " AND ".join(where)
        by_sat = dict(
            session.execute(
                text(f"SELECT f.satellite, count(*) FROM fire_detection f WHERE {sql_where} "
                     "GROUP BY 1"),  # fmt: skip
                params,
            ).all()
        )
        rows = session.execute(
            text(
                f"""
                SELECT f.id, f.satellite, f.instrument, f.source, f.acq_utc, f.confidence,
                       f.frp_mw, f.daynight, f.state, f.district, f.lon, f.lat, f.scan, f.track
                FROM fire_detection f WHERE {sql_where}
                ORDER BY f.acq_utc DESC, f.id LIMIT :limit
                """
            ),
            {**params, "limit": limit},
        ).mappings()
        features = [
            {
                "type": "Feature",
                "id": r["id"],
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {
                    "id": r["id"],
                    "satellite": r["satellite"],
                    "instrument": r["instrument"],
                    "source": r["source"],
                    "acq_utc": r["acq_utc"],
                    "confidence": r["confidence"],
                    "frp_mw": r["frp_mw"],
                    "daynight": r["daynight"],
                    "state": r["state"],
                    "district": r["district"],
                    "scan_km": r["scan"],
                    "track_km": r["track"],
                },
            }
            for r in rows
        ]
        count = sum(by_sat.values())
        payload = {
            "hours": hours,
            "count": count,
            "by_satellite": {s: by_sat.get(s, 0) for s in fires_stats.FIRE_SATELLITES}
            | {s: n for s, n in by_sat.items() if s not in fires_stats.FIRE_SATELLITES},
            "window": {"from_utc": t0, "to_utc": t1},
            "latest_acq_utc": features[0]["properties"]["acq_utc"] if features else None,
            "region": region,
            "clip": clip,
            "min_confidence": min_confidence,
            "truncated": count > len(features),
            "note": note,
            "features": {"type": "FeatureCollection", "features": features},
        }
        return payload, fires_stats.fire_refs(session, sql_where, params)

    payload, sources = _cached(key, build)
    return respond(payload, sources, max_age=CACHE_S)


def _ist_today() -> date:
    return utcnow().astimezone(IST).date()


def _day_range(since: date | None, until: date | None) -> tuple[date, date]:
    today = _ist_today()
    last = until or today
    first = since or fires_stats.season_start(last)
    if first > last:
        raise HTTPException(400, "since must be on or before until")
    if (last - first).days > 400:
        raise HTTPException(400, "at most 400 days per request")
    return first, last


@router.get("/daily")
def daily(
    session: SessionDep,
    since: date | None = Query(None, description="first IST day; default the season start (15 Sep)"),
    until: date | None = Query(None, description="last IST day; default today (IST)"),
    region: Region = "all",
) -> ORJSONResponse:  # fmt: skip
    """Detections and fire radiative power per IST day. `complete` marks days whose detections
    have all arrived (fetched through the area API a full day after they ended)."""
    first, last = _day_range(since, until)
    payload, sources = _cached(
        ("daily", first, last, region),
        lambda: fires_stats.daily_series(session, first, last, region),
    )
    return respond(payload, sources, max_age=CACHE_S)


@router.get("/hours")
def hours(
    session: SessionDep,
    days: int = Query(7, ge=1, le=90),
    region: Region = "all",
    end: datetime | None = Query(None, description="window end (ISO-8601, UTC); default now"),
) -> ORJSONResponse:
    """Detections by IST hour of day over the last `days` days, per satellite; the IST windows in
    which satellites reported fires and the longest span between them, from the data itself."""
    t1 = _as_utc(end)
    payload, sources = _cached(
        ("hours", days, region, end and t1),
        lambda: fires_stats.hours_profile(session, days, region, t1),
    )
    return respond(payload, sources, max_age=CACHE_S)


@router.get("/official")
def official(
    session: SessionDep,
    since: date | None = Query(None, description="first IST day; default the season start (15 Sep)"),
    until: date | None = Query(None, description="last IST day; default today (IST)"),
    region: Region = "all",
) -> ORJSONResponse:  # fmt: skip
    """CREAMS (ICAR-IARI) official residue-burning events per IST day, by satellite, state and
    district: the count India officially reports, to set beside FIRMS and Sentinel-2."""
    first, last = _day_range(since, until)
    payload, sources = _cached(
        ("official", first, last, region),
        lambda: fires_stats.official_counts(session, first, last, region),
    )
    return respond(payload, sources, max_age=CACHE_S)
