"""Burn scars: the Sentinel-2 clusters, a season summary per district, and one scar's case file.

GET /api/scars              scars as GeoJSON polygons, with totals (filters: since, until, district,
                            unseen, bbox, limit)
GET /api/scars/summary      season totals and a per-district table, with FIRMS counts beside them
GET /api/scars/{id}         one scar: its properties and outline, before/after imagery links, the
                            fire detections around it and the weather while it burnt

Every number is read from PostGIS (burn_cluster, fire_detection, met_grid), which the sentinel,
fires and met jobs fill. Scars are *candidates*: a field that darkened and now looks charred
between two clear passes. Tillage can mimic that, so the UI must show the method beside the number.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Annotated, Any
from urllib.parse import urlencode

import orjson
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics.burnscar import RULE, audit_summary
from blindspot.api import BBoxDep, SessionDep, respond
from blindspot.api.cache import (
    cached_route,
    canonical_key,
    code_version,
    content_stamp,
    stored_response,
)
from blindspot.api.responses import ORJSONResponse
from blindspot.config import get_settings
from blindspot.geo import parse_bbox
from blindspot.models.sentinel import S2Scene
from blindspot.provenance import (
    SourceRef,
    label_for,
    latest_refs,
    ref_for_fetch,
    utcnow,
)

router = APIRouter(prefix="/api/scars", tags=["scars"])

SCAR_SOURCES = ["stac_e84", "s2_cog", "worldcover", "firms", "boundaries"]
# STAC catalogues of the scenes (as in sources/stac.py, which is not imported here: it loads
# rasterio, which the API must not load at startup)
E84_STAC = "https://earth-search.aws.element84.com/v1"
E84_COLLECTION = "sentinel-2-c1-l2a"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
MAX_LIMIT = 20_000
CHIP_MIN_M = 1200.0  # imagery chips show at least 1.2 km around a scar, for context
CHIP_PX = 512
NEARBY_KM = 5.0  # fire detections listed around a scar
NEARBY_DAYS = 2  # ...from this many days before the clear pass to this many after the burnt one
KM_PER_DEG = 111.32  # length of one degree of latitude, km

_FEATURE_COLUMNS = """
    id, area_ha, burn_after, burn_before, gap_days, district, subdistrict, village, village_km,
    state, ST_X(centroid) AS lon, ST_Y(centroid) AS lat, dnbr, nbr_post, confidence, unseen,
    fire_matches, nearest_fire_m, scene_pre, scene_post, collection, res_m, mgrs_tile, hazy,
    model_version
"""


def _iso_day(value: dt.date | None, end_of_day: bool = False) -> dt.datetime | None:
    if value is None:
        return None
    t = dt.time.max if end_of_day else dt.time.min
    return dt.datetime.combine(value, t, dt.UTC)


def _filters(
    since: dt.date | None, until: dt.date | None, district: str | None, unseen: bool | None,
    bbox: tuple[float, float, float, float] | None, min_confidence: float | None = None,
) -> tuple[str, dict[str, Any]]:
    where = ["model_version = :version"]
    params: dict[str, Any] = {"version": RULE.version}
    if min_confidence is not None:
        where.append("confidence >= :min_conf")
        params["min_conf"] = min_confidence
    if since is not None:
        where.append("burn_before >= :since")
        params["since"] = _iso_day(since)
    if until is not None:
        where.append("burn_before <= :until")
        params["until"] = _iso_day(until, end_of_day=True)
    if district:
        where.append("lower(district) = lower(:district)")
        params["district"] = district.strip()
    if unseen is not None:
        where.append("unseen = :unseen")
        params["unseen"] = unseen
    if bbox is not None:
        where.append("geom && ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326)")
        params.update(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3])
    return " AND ".join(where), params


def _properties(row: dict[str, Any]) -> dict[str, Any]:
    """The contract's feature properties, plus a few documented additions."""
    return {
        "id": row["id"],
        "area_ha": row["area_ha"],
        "burn_after": row["burn_after"],
        "burn_before": row["burn_before"],
        "district": row["district"],
        "subdistrict": row["subdistrict"],
        "village": row["village"],
        "centroid": [round(row["lon"], 6), round(row["lat"], 6)],
        "dnbr": row["dnbr"],
        "confidence": row["confidence"],
        "unseen": row["unseen"],
        "label": _label(row["unseen"]),  # addition: the wording the UI must use
        "fire_matches": row["fire_matches"],
        "scene_pre": row["scene_pre"],
        "scene_post": row["scene_post"],
        # additions
        "state": row["state"],
        "village_km": row["village_km"],
        "gap_days": row["gap_days"],
        "nbr_post": row["nbr_post"],
        "nearest_fire_m": row["nearest_fire_m"],
        "hazy": row["hazy"],
        "res_m": row["res_m"],
        "mgrs_tile": row["mgrs_tile"],
        "collection": row["collection"],
    }


def _label(unseen: bool | None) -> str:
    """UI wording. Every scar stays a *candidate* until audited; "unseen" only means no FIRMS
    detection explains it (SYNTHESIS build-time update: "candidate unseen burn")."""
    if unseen is None:
        return "candidate burn (no fire-record verdict yet)"
    return "candidate unseen burn" if unseen else "candidate burn, FIRMS-confirmed"


def _sources(session: Session) -> list[SourceRef]:
    return latest_refs(session, SCAR_SOURCES)


def _scene_ref(session: Session, item_id: str | None, collection: str | None) -> SourceRef | None:
    """One of a case's own two Sentinel-2 scenes: its STAC item, dated by the catalogue fetch that
    found it."""
    if not item_id:
        return None
    scene = session.get(S2Scene, item_id)
    coll = (scene.collection if scene is not None else None) or collection or E84_COLLECTION
    found = ref_for_fetch(session, scene.fetch_id) if scene is not None else None
    key = found.key if found is not None else ("stac_e84" if coll == E84_COLLECTION else "stac_pc")
    base = E84_STAC if coll == E84_COLLECTION else PC_STAC
    return SourceRef(key=key, label=label_for(key), url=f"{base}/collections/{coll}/items/{item_id}",
                     fetched_at=found.fetched_at if found else None, fetch_id=found.fetch_id if found else None)


_BBOX_IN_URL = re.compile(r"bbox=(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)")


def _cropland_ref(session: Session, lon: float | None, lat: float | None) -> SourceRef | None:
    """The newest ESA WorldCover search whose box covers the case: the cropland mask its tile was
    read with (each search's URL carries its box)."""
    if lon is None or lat is None:
        return None
    rows = session.execute(text(
        "SELECT id, url FROM source_fetch WHERE ok AND source_key = 'worldcover' ORDER BY started_at DESC LIMIT 200"
    )).all()
    for fid, url in rows:
        m = _BBOX_IN_URL.search(url or "")
        if m and float(m[1]) <= lon <= float(m[3]) and float(m[2]) <= lat <= float(m[4]):
            return ref_for_fetch(session, fid)
    return None


def _case_sources(session: Session, row: dict[str, Any], weather_fetch: int | None) -> list[SourceRef]:
    """What one case file rests on: its own two scenes, the processing run that drew its outline, the
    cropland mask read for its area, the fire records it was matched against, the boundaries that
    name it and the weather grid. Not the newest fetch of each source, which may be another tile's
    scene or another area's mask."""
    refs = [
        _scene_ref(session, row["scene_pre"], row["collection"]),
        _scene_ref(session, row["scene_post"], row["collection"]),
        ref_for_fetch(session, row["fetch_id"]),
        _cropland_ref(session, row["lon"], row["lat"]),
        *latest_refs(session, ["firms", "boundaries"]),
        ref_for_fetch(session, weather_fetch),
    ]
    return [r for r in refs if r is not None]


@router.get("")
@cached_route(ttl=60)
def scars(
    session: SessionDep,
    bbox: BBoxDep,
    since: Annotated[dt.date | None, Query(description="first burnt pass on/after (UTC date)")] = None,
    until: Annotated[dt.date | None, Query(description="first burnt pass on/before (UTC date)")] = None,
    district: str | None = None,
    unseen: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 5000,
    min_confidence: Annotated[float | None, Query(ge=0, le=1, description="addition: drop weaker candidates")] = None,
) -> ORJSONResponse:
    """Burn-scar polygons (newest first, then largest) and totals over everything that matched.

    `unseen` is true when no fire detection explains the scar, false when one does, and null while
    no fire records cover its window. Totals count all matching scars even when `limit` cuts the list."""
    if until is not None and until >= utcnow().date():
        # Every scar was seen by today, so this answer is the one without `until` (the payload
        # does not echo it): one stored key instead of one per future date.
        until = None
    key = canonical_key("scars", bbox=bbox, since=since, until=until,
                        district=district.strip().lower() if district else None, unseen=unseen,
                        limit=limit, min_confidence=min_confidence)
    return stored_response(
        session, key, scars_stamp(session, since, until, district, unseen, bbox, min_confidence),
        lambda: _scars_payload(session, bbox, since, until, district, unseen, limit, min_confidence),
        live_sources=lambda: _sources(session),
        guard=limit > GUARD_LIMIT and not _website_family(bbox, since, until, district, unseen, limit,
                                                          min_confidence))


# The live map's list (data.ts SCAR_QUERY) with one replay day or one clicked district: a family
# the website walks through (Play steps a day every 1.4 s), so it is never refused as novel. Its
# keys are bounded: `until` up to today (later dates are the plain list), real district names.
WEBSITE_LIST = {"limit": 2500, "min_confidence": 0.5}


def _website_family(
    bbox: tuple[float, float, float, float] | None, since: dt.date | None, until: dt.date | None,
    district: str | None, unseen: bool | None, limit: int, min_confidence: float | None,
) -> bool:
    return (limit == WEBSITE_LIST["limit"] and min_confidence == WEBSITE_LIST["min_confidence"]
            and tuple(bbox or ()) == tuple(parse_bbox(None)) and since is None and unseen is None
            and not (until and district))


# Scar lists run to megabytes of polygons (the live map's 2,500: 1.4 MB of rows for 2.4 MB of JSON),
# so the finished answer is stored (api/cache.stored_response) until a row the query selects
# changes in a column the answer shows, or the code that renders it does. The stamp is computed by
# Postgres over exactly those rows and columns (cache.content_stamp), not over the whole table:
# the sentinel job rewrites every row's matched_at daily and adds new scars, but a season-replay
# day (`until=`) keeps its stored answer for as long as its own scars and verdicts are unchanged.
# Sources are added per request (the newest FIRMS fetch changes hourly; the polygons do not).
_SCAR_RENDERERS = ("blindspot.api.routers.scars", "blindspot.analytics.burnscar")
# Every column _FEATURE_COLUMNS and the totals read, and the outline.
_STAMP_COLUMNS = ("id, area_ha, burn_after, burn_before, gap_days, district, subdistrict, village, "
                  "village_km, state, centroid, dnbr, nbr_post, confidence, unseen, fire_matches, "
                  "nearest_fire_m, scene_pre, scene_post, collection, res_m, mgrs_tile, hazy, "
                  "model_version, geom")
GUARD_LIMIT = 200  # lists longer than this are costly: a never-stored variant counts as novel


def scars_stamp(
    session: Session, since: dt.date | None = None, until: dt.date | None = None,
    district: str | None = None, unseen: bool | None = None,
    bbox: tuple[float, float, float, float] | None = None, min_confidence: float | None = None,
) -> str:
    where, params = _filters(since, until, district, unseen, bbox, min_confidence)
    return (f"{content_stamp(session, 'burn_cluster', _STAMP_COLUMNS, where, params)}"
            f"|{code_version(*_SCAR_RENDERERS)}")


def _scars_payload(
    session: Session, bbox: tuple[float, float, float, float] | None, since: dt.date | None,
    until: dt.date | None, district: str | None, unseen: bool | None, limit: int,
    min_confidence: float | None,
) -> tuple[dict[str, Any], list[SourceRef], int]:
    where, params = _filters(since, until, district, unseen, bbox, min_confidence)
    totals = session.execute(text(f"""
        SELECT count(*) AS clusters, coalesce(sum(area_ha), 0) AS area_ha,
               count(*) FILTER (WHERE unseen) AS unseen_clusters,
               coalesce(sum(area_ha) FILTER (WHERE unseen), 0) AS unseen_area_ha,
               count(*) FILTER (WHERE unseen IS NOT NULL) AS checked_clusters,
               min(burn_after) AS first_pass, max(burn_before) AS last_pass
        FROM burn_cluster WHERE {where}"""), params).mappings().one()
    rows = session.execute(text(f"""
        SELECT {_FEATURE_COLUMNS}, ST_AsGeoJSON(geom, 6)::json AS geometry
        FROM burn_cluster WHERE {where}
        ORDER BY burn_before DESC, area_ha DESC, id
        LIMIT :limit"""), {**params, "limit": limit}).mappings().all()
    features = [
        {"type": "Feature", "id": r["id"], "geometry": r["geometry"], "properties": _properties(r)}
        for r in rows
    ]
    payload = {
        "totals": {
            "clusters": totals["clusters"],
            "area_ha": round(float(totals["area_ha"]), 2),
            "unseen_clusters": totals["unseen_clusters"],
            "unseen_area_ha": round(float(totals["unseen_area_ha"]), 2),
            "checked_clusters": totals["checked_clusters"],  # addition: clusters with a fire verdict
        },
        "returned": len(features),
        "truncated": len(features) < totals["clusters"],
        "window": {"first_pass": totals["first_pass"], "last_pass": totals["last_pass"]},
        "method": _method(),
        "features": {"type": "FeatureCollection", "features": features},
    }
    return payload, _sources(session), 60


def _method() -> dict[str, Any]:
    return {
        "model_version": RULE.version,
        "summary": (
            "Sentinel-2 L2A, cropland only: NIR and SWIR darkened since the previous clear pass "
            f"(dB8A>{RULE.d_b8a}, dB11>{RULE.d_b11}, dB12>{RULE.d_b12}) and the field now looks "
            f"charred (BAIS2>={RULE.bais2_min}, brightness<={RULE.tbi_max}); passes at most "
            f"{RULE.max_gap_days} days apart; clusters of at least {RULE.min_area_ha} ha whose mean "
            f"post-burn NBR is below {RULE.nbr_post_max} (drops flooded fields and cloud shadows). "
            "Candidates: tillage can look similar."
        ),
        "label": "candidate unseen burn",
        "audit": audit_summary(),
        "unseen_rule": (
            "unseen = no FIRMS detection within max(1 km, pixel half-diagonal + 200 m) of the scar "
            "between the clear pass before it and 3 h after the pass that shows it"
        ),
    }


def _season_start(today: dt.date) -> dt.date:
    year = today.year if today.month >= 7 else today.year - 1
    return dt.date(year, 10, 1)


def _table_exists(session: Session, name: str) -> bool:
    return session.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}).scalar() is not None


@router.get("/summary")
@cached_route(ttl=60)
def summary(
    session: SessionDep,
    since: Annotated[dt.date | None, Query(description="default: 1 October of this season")] = None,
) -> ORJSONResponse:
    """Season totals and a per-district table: scarred area, how much of it no fire detection
    explains, and how many FIRMS detections the same district had (Indian polygons only)."""
    start = since or _season_start(dt.datetime.now(dt.UTC).date())
    params = {"version": RULE.version, "since": _iso_day(start)}
    season = session.execute(text("""
        SELECT count(*) AS clusters, coalesce(sum(area_ha), 0) AS area_ha,
               coalesce(sum(area_ha) FILTER (WHERE unseen), 0) AS unseen_area_ha,
               coalesce(sum(area_ha) FILTER (WHERE unseen IS NOT NULL), 0) AS checked_area_ha,
               max(burn_before) AS last_pass
        FROM burn_cluster WHERE model_version = :version AND burn_before >= :since"""), params).mappings().one()
    districts = session.execute(text("""
        SELECT district, max(state) AS state, count(*) AS clusters, sum(area_ha) AS area_ha,
               coalesce(sum(area_ha) FILTER (WHERE unseen), 0) AS unseen_area_ha,
               coalesce(sum(area_ha) FILTER (WHERE unseen IS NOT NULL), 0) AS checked_area_ha,
               array_agg(DISTINCT scope) AS scopes, max(res_m) AS res_m
        FROM burn_cluster WHERE model_version = :version AND burn_before >= :since
        GROUP BY district ORDER BY sum(area_ha) DESC"""), params).mappings().all()
    fires_total: int | None = None
    fires_by_district: dict[str, int] = {}
    if _table_exists(session, "fire_detection"):
        rows = session.execute(text("""
            SELECT district, count(*) AS n FROM fire_detection
            WHERE acq_utc >= :since AND state IN ('Punjab', 'Haryana') GROUP BY district"""),
            {"since": params["since"]}).all()
        fires_by_district = {d: n for d, n in rows if d}
        fires_total = sum(n for _, n in rows)

    def share(unseen_ha: float, checked_ha: float) -> float | None:
        return round(unseen_ha / checked_ha, 4) if checked_ha else None

    by_district = [
        {
            "district": d["district"],
            "state": d["state"],
            "area_ha": round(float(d["area_ha"]), 2),
            "clusters": d["clusters"],
            "unseen_area_ha": round(float(d["unseen_area_ha"]), 2),
            "unseen_share": share(float(d["unseen_area_ha"]), float(d["checked_area_ha"])),
            "fire_detections": fires_by_district.get(d["district"], 0) if fires_total is not None else None,
            "scope": sorted(s for s in d["scopes"] if s),  # addition: processing stage(s)
            "res_m": d["res_m"],  # addition
        }
        for d in districts
    ]
    payload = {
        "season": {
            "since": start,
            "clusters": season["clusters"],
            "area_ha": round(float(season["area_ha"]), 2),
            "unseen_area_ha": round(float(season["unseen_area_ha"]), 2),
            "unseen_share": share(float(season["unseen_area_ha"]), float(season["checked_area_ha"])),
            # Only districts that have scars: processing covers some tiles, not the whole state.
            "fire_detections": (sum(fires_by_district.get(d["district"], 0) for d in districts)
                                if fires_total is not None else None),
            "fire_detections_scope": "FIRMS detections in the districts listed in by_district (addition)",
            "checked_area_ha": round(float(season["checked_area_ha"]), 2),  # addition
            "last_pass": season["last_pass"],  # addition
        },
        "by_district": by_district,
        "coverage": _coverage(session),
        "method": _method(),
    }
    return respond(payload, _sources(session), max_age=60)


def _coverage(session: Session) -> dict[str, Any]:
    """Which passes have been analysed: so a reader knows what the totals cover."""
    if not _table_exists(session, "s2_scene"):
        return {}
    row = session.execute(text("""
        SELECT count(*) FILTER (WHERE status = 'processed') AS processed,
               count(*) FILTER (WHERE status = 'skipped') AS skipped,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) AS catalogued,
               max(datetime) FILTER (WHERE status = 'processed') AS latest_processed_pass,
               array_agg(DISTINCT mgrs_tile) FILTER (WHERE status = 'processed') AS tiles
        FROM s2_scene""")).mappings().one()
    out = dict(row)
    out["tiles"] = sorted(out["tiles"] or [])
    return out


# --- one scar -------------------------------------------------------------------------------------


def _chip_bbox(geom: dict[str, Any], lat: float) -> tuple[float, float, float, float]:
    """A square lon/lat box around the scar: its extent plus a margin, at least CHIP_MIN_M wide."""
    coords = [pt for ring in geom["coordinates"] for pt in ring]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    km_lon = KM_PER_DEG * math.cos(math.radians(lat))
    width_m = max((max(lons) - min(lons)) * km_lon * 1000, (max(lats) - min(lats)) * KM_PER_DEG * 1000)
    side_m = max(CHIP_MIN_M, width_m * 1.6)
    cx, cy = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    half_lon = side_m / 2 / (km_lon * 1000)
    half_lat = side_m / 2 / (KM_PER_DEG * 1000)
    return (round(cx - half_lon, 6), round(cy - half_lat, 6), round(cx + half_lon, 6), round(cy + half_lat, 6))


def render_url(collection: str, item: str, bbox: tuple[float, float, float, float], kind: str) -> str:
    """GET /api/imagery/render (served by the imagery module) with the contract's parameters."""
    query = urlencode({
        "collection": collection, "item": item, "bbox": ",".join(f"{v:.6f}" for v in bbox),
        "kind": kind, "w": CHIP_PX, "h": CHIP_PX,
    })
    return f"{get_settings().public_base_url}/api/imagery/render?{query}"


def _imagery(session: Session, row: dict[str, Any], bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    out: dict[str, Any] = {"bbox": list(bbox)}
    for key, item in (("pre", row["scene_pre"]), ("post", row["scene_post"])):
        scene = session.execute(text(
            "SELECT datetime, cloud_pct, mgrs_tile FROM s2_scene WHERE item_id = :i"), {"i": item}
        ).mappings().first()
        when = scene["datetime"] if scene else (row["burn_after"] if key == "pre" else row["burn_before"])
        out[key] = {
            "date": when.date().isoformat() if when else None,
            "datetime": when,
            "item_id": item,
            "collection": row["collection"],
            "cloud_pct": scene["cloud_pct"] if scene else None,
            "truecolor_url": render_url(row["collection"], item, bbox, "truecolor"),
            "swir_url": render_url(row["collection"], item, bbox, "swir"),
            "nbr_url": render_url(row["collection"], item, bbox, "nbr"),
        }
    return out


def _fires_nearby(session: Session, scar_id: int) -> list[dict[str, Any]]:
    if not _table_exists(session, "fire_detection"):
        return []
    rows = session.execute(text("""
        SELECT f.id, f.source, f.satellite, f.instrument, f.acq_utc, f.lat, f.lon, f.confidence,
               f.frp_mw, f.daynight, f.scan, f.track,
               ST_Distance(b.geom::geography, f.geom::geography) AS distance_m,
               GREATEST(1000.0, 500.0 * sqrt(coalesce(f.scan, 0)^2 + coalesce(f.track, 0)^2) + 200.0)
                   AS match_radius_m,
               (f.acq_utc >= b.burn_after AND f.acq_utc <= b.burn_before + interval '3 hours') AS in_window
        FROM burn_cluster b JOIN fire_detection f
          ON f.geom && ST_Expand(b.geom, :deg)
         AND f.acq_utc >= b.burn_after - make_interval(days => :days)
         AND f.acq_utc <= b.burn_before + make_interval(days => :days)
        WHERE b.id = :id AND ST_DWithin(b.geom::geography, f.geom::geography, :m)
        ORDER BY distance_m, f.acq_utc LIMIT 100"""),
        {"id": scar_id, "deg": NEARBY_KM / 100, "days": NEARBY_DAYS, "m": NEARBY_KM * 1000}).mappings().all()
    return [
        {
            "id": r["id"], "source": r["source"], "satellite": r["satellite"], "instrument": r["instrument"],
            "acq_utc": r["acq_utc"], "lat": r["lat"], "lon": r["lon"], "confidence": r["confidence"],
            "frp_mw": r["frp_mw"], "daynight": r["daynight"],
            "distance_m": round(float(r["distance_m"]), 1),
            "match_radius_m": round(float(r["match_radius_m"]), 1),
            "in_window": bool(r["in_window"]),
            "explains_scar": bool(r["in_window"]) and float(r["distance_m"]) <= float(r["match_radius_m"]),
        }
        for r in rows
    ]


def _bilinear(values: list[float] | None, nx: int, fx: float, fy: float) -> float | None:
    """Value at fractional grid position (fx, fy) of a row-major (j * nx + i) grid."""
    if not values:
        return None
    i0, j0 = int(math.floor(fx)), int(math.floor(fy))
    tx, ty = fx - i0, fy - j0
    total, weight = 0.0, 0.0
    for di, dj, w in ((0, 0, (1 - tx) * (1 - ty)), (1, 0, tx * (1 - ty)), (0, 1, (1 - tx) * ty), (1, 1, tx * ty)):
        k = (j0 + dj) * nx + (i0 + di)
        if 0 <= k < len(values) and values[k] is not None and not math.isnan(values[k]) and w > 0:
            total += values[k] * w
            weight += w
    return total / weight if weight else None


class _Corners:
    """The four grid cells around a point, standing in for a whole row-major grid in _bilinear:
    its length is the grid's, and only the corner indices are held."""

    __slots__ = ("items", "n")

    def __init__(self, n: int, items: dict[int, float | None]) -> None:
        self.n, self.items = n, items

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, k: int) -> float | None:
        return self.items[k]


_GRID_FIELDS = ("u", "v", "blh", "t", "precip")


def _grid_corners(
    session: Session, row: dict[str, Any], valid_at: dt.datetime | None = None, whole: bool = False
) -> list[dict[str, Any]]:
    """The newest best_match 10 m grid of each hour of the burn window, with only the four cells
    around the scar: a case file used to read 72 whole grids (~535 KB) for one point.

    Postgres works out the cell (floor of the same IEEE double division as _weather) and returns
    the four values of each field; an index outside the grid comes back NULL, as _bilinear skips
    it. `whole` reads the full arrays of one hour instead (a fallback that never triggers)."""
    if whole:
        out = session.execute(text("""
            SELECT valid_at, run_at, lon0, lat0, dlon, dlat, nx, ny, u, v, blh, t, precip, fetch_id
            FROM met_grid WHERE level = '10m' AND model = 'best_match' AND valid_at = :t
            ORDER BY run_at DESC LIMIT 1"""), {"t": valid_at}).mappings().all()
        return [dict(r) for r in out]
    corners = ("k00", "k10", "k01", "k11")
    picks = ", ".join(f"cardinality(g.{f}) AS n_{f}, " + ", ".join(f"g.{f}[c.{k} + 1] AS {f}_{k}" for k in corners)
                      for f in _GRID_FIELDS)
    rows = session.execute(text(f"""
        SELECT DISTINCT ON (g.valid_at) g.valid_at, g.run_at, g.lon0, g.lat0, g.dlon, g.dlat, g.nx, g.ny,
               g.fetch_id, c.i0, c.j0, c.k00, c.k10, c.k01, c.k11, {picks}
        FROM met_grid g
        CROSS JOIN LATERAL (
            SELECT floor((CAST(:lon AS double precision) - g.lon0) / g.dlon)::int AS i0,
                   floor((CAST(:lat AS double precision) - g.lat0) / g.dlat)::int AS j0) ij
        CROSS JOIN LATERAL (
            SELECT ij.i0, ij.j0, ij.j0 * g.nx + ij.i0 AS k00, ij.j0 * g.nx + ij.i0 + 1 AS k10,
                   (ij.j0 + 1) * g.nx + ij.i0 AS k01, (ij.j0 + 1) * g.nx + ij.i0 + 1 AS k11) c
        WHERE g.level = '10m' AND g.model = 'best_match' AND g.valid_at >= :a AND g.valid_at <= :b
        ORDER BY g.valid_at, g.run_at DESC"""),
        {"lon": row["lon"], "lat": row["lat"], "a": row["burn_after"], "b": row["burn_before"]}).mappings().all()
    out = []
    for r in rows:
        g = {k: r[k] for k in ("valid_at", "run_at", "lon0", "lat0", "dlon", "dlat", "nx", "ny", "fetch_id",
                               "i0", "j0")}
        for f in _GRID_FIELDS:
            n = r[f"n_{f}"]
            g[f] = None if n is None else _Corners(n, {r[k]: r[f"{f}_{k}"] for k in corners})
        out.append(g)
    return out


def _weather(session: Session, row: dict[str, Any]) -> dict[str, Any]:
    """10 m wind, boundary-layer height, temperature and rain at the scar while it could have burnt,
    from the met module's grids (best_match model). Says so when no grid covers the window."""
    window = {"from": row["burn_after"], "to": row["burn_before"]}
    if not _table_exists(session, "met_grid"):
        return {"available": False, "reason": "no weather grids stored yet (met job)", "window": window}
    grids = _grid_corners(session, row)
    if not grids:
        return {"available": False, "reason": "no stored weather grid covers this burn window", "window": window}
    series = []
    for g in grids:
        fx = (row["lon"] - g["lon0"]) / g["dlon"]
        fy = (row["lat"] - g["lat0"]) / g["dlat"]
        if not (0 <= fx <= g["nx"] - 1 and 0 <= fy <= g["ny"] - 1):
            continue
        if (int(math.floor(fx)), int(math.floor(fy))) != (g["i0"], g["j0"]):  # never in IEEE arithmetic
            g = _grid_corners(session, row, valid_at=g["valid_at"], whole=True)[0]
        u = _bilinear(g["u"], g["nx"], fx, fy)
        v = _bilinear(g["v"], g["nx"], fx, fy)
        speed = None if u is None or v is None else 3.6 * math.hypot(u, v)
        from_deg = None if u is None or v is None else (math.degrees(math.atan2(-u, -v)) + 360) % 360
        series.append({
            "t": g["valid_at"],
            "wind_10m_kmh": None if speed is None else round(speed, 1),
            "wind_from_deg": None if from_deg is None else round(from_deg),
            "blh_m": _round(_bilinear(g["blh"], g["nx"], fx, fy), 0),
            "temp_2m_c": _round(_bilinear(g["t"], g["nx"], fx, fy), 1),
            "precip_mm": _round(_bilinear(g["precip"], g["nx"], fx, fy), 2),
        })
    if not series:
        return {"available": False, "reason": "the scar lies outside the stored weather grid", "window": window}
    hours = len(series)
    # Burning happens in the afternoon and evening (12:30-19:30 IST = 07-14 UTC).
    afternoon = [s for s in series if 7 <= s["t"].hour <= 14]
    winds = [s["wind_10m_kmh"] for s in afternoon if s["wind_10m_kmh"] is not None]
    blhs = [s["blh_m"] for s in afternoon if s["blh_m"] is not None]
    rain = [s["precip_mm"] for s in series if s["precip_mm"] is not None]
    covered = series[-1]["t"] - series[0]["t"]
    return {
        "available": True,
        "model": "best_match",
        "window": window,
        "hours": hours,
        "covered_hours": round(covered.total_seconds() / 3600) + 1,
        "afternoon_wind_10m_kmh_mean": round(sum(winds) / len(winds), 1) if winds else None,
        "afternoon_blh_m_mean": round(sum(blhs) / len(blhs)) if blhs else None,
        "rain_mm_total": round(sum(rain), 1) if rain else None,
        "series": series[::3],  # 3-hourly is plenty for a case file
        "fetch_id": grids[0]["fetch_id"],
    }


def _round(value: float | None, ndigits: int) -> float | None:
    return None if value is None else round(value, ndigits)


@router.get("/{scar_id}")
def scar(scar_id: int, session: SessionDep) -> ORJSONResponse:
    """One scar's case file: properties and outline, before/after imagery (true colour, SWIR, NBR),
    fire detections within 5 km around its window, and the weather while it could have burnt."""
    row = session.execute(text(f"""
        SELECT {_FEATURE_COLUMNS}, ST_AsGeoJSON(geom, 6)::json AS geometry, n_px, d_b8a, d_b11, d_b12,
               bais2_post, tbi_post, ndvi_pre, ndvi_post, matched_at, fetch_id, created_at
        FROM burn_cluster WHERE id = :id"""), {"id": scar_id}).mappings().first()
    if row is None:
        raise HTTPException(404, "no such scar")
    row = dict(row)
    geometry = row.pop("geometry")
    if isinstance(geometry, str):
        geometry = orjson.loads(geometry)
    case = {
        **_properties(row),
        "geometry": geometry,
        "evidence": {k: row[k] for k in ("n_px", "d_b8a", "d_b11", "d_b12", "bais2_post", "tbi_post",
                                         "ndvi_pre", "ndvi_post")},
        "matched_at": row["matched_at"],
        "model_version": row["model_version"],
        "created_at": row["created_at"],
    }
    bbox = _chip_bbox(geometry, row["lat"])
    weather = _weather(session, row)
    sources = _case_sources(session, row, weather.get("fetch_id"))
    payload = {
        "case": case,
        "imagery": _imagery(session, row, bbox),
        "fires_nearby": _fires_nearby(session, scar_id),
        "weather_at_burn": weather,
        "method": _method(),
    }
    return respond(payload, sources, max_age=60)
