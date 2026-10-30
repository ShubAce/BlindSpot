"""Imagery: Sentinel-2 pictures (crops and map tiles) and the landing page's featured frames.

GET /api/imagery/render                         one picture of an item over a bbox
GET /api/imagery/tiles/{kind}/{item}/{z}/{x}/{y}  the same recipes as XYZ map tiles (contract addition)
GET /api/imagery/tilejson/{kind}/{item}          TileJSON for that tile layer (contract addition)
GET /api/imagery/featured                        the 6.4 x 4 km window of Indian Punjab with the most
                                                 burning, and its clear passes of the last 20 days

Pictures come from blindspot.sources.pcdata: the Planetary Computer tiler, or our own renderer when
PC's pixels fail the plausibility gate. Every picture is checked before it is served and kept on disk
forever, so the routes send immutable cache headers. A failure is always a JSON error, never a
placeholder image. Requests are bounded (area, pixels, collections, the area we watch) so the proxy
cannot be used to fetch arbitrary imagery at our expense.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import numpy as np
import orjson
from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import Response
from shapely.geometry import box, mapping, shape
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.api import SessionDep, respond
from blindspot.api.responses import ORJSONResponse
from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.geo import AOI_BBOX, district_for_point, nearest_place, parse_bbox
from blindspot.http import FetchError
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch, utcnow
from blindspot.sources import pcdata

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/imagery", tags=["imagery"])

IMMUTABLE = "public, max-age=31536000, immutable"
MAX_SIDE = 2048
MAX_PIXELS = 2048 * 1024  # 2.1 MP: enough for the 1600 x 1000 landing frames
MAX_BBOX_KM2 = 900.0  # a 30 x 30 km crop; scar chips are ~1 km, the featured window 25.6 km2
AOI_MARGIN_DEG = 0.25  # requests must stay within the area we watch, plus this margin
MIN_TILE_Z, MAX_TILE_Z = 9, 16  # z9 ~ 300 m/px; Sentinel-2's 10 m is about z14, z15-16 overzoom
CROP_MIN_VALID = 0.5  # half of a requested crop must have data (a scene edge may cut a chip)
KM_PER_DEG = 111.32  # length of one degree of latitude (and of longitude at the equator), km


def _km_per_deg_lon(lat: float) -> float:
    return KM_PER_DEG * math.cos(math.radians(lat))


def _bbox_km2(b: tuple[float, float, float, float]) -> float:
    mid = (b[1] + b[3]) / 2
    return (b[2] - b[0]) * _km_per_deg_lon(mid) * (b[3] - b[1]) * KM_PER_DEG


def _inside_watch_area(b: tuple[float, float, float, float]) -> bool:
    a = AOI_BBOX
    m = AOI_MARGIN_DEG
    return a[0] - m <= b[0] and a[1] - m <= b[1] and b[2] <= a[2] + m and b[3] <= a[3] + m


def _overlaps_watch_area(b: tuple[float, float, float, float]) -> bool:
    a = AOI_BBOX
    m = AOI_MARGIN_DEG
    return b[0] < a[2] + m and b[2] > a[0] - m and b[1] < a[3] + m and b[3] > a[1] - m


def _round_bbox(b: Iterable[float]) -> tuple[float, float, float, float]:
    """6 decimals (~0.1 m): the same bbox always maps to the same cache entry."""
    r = tuple(round(float(v), 6) for v in b)
    return (r[0], r[1], r[2], r[3])


def bbox_param(b: Iterable[float]) -> str:
    return ",".join(f"{v:.6f}" for v in b)


def render_url(collection: str, item: str, bbox: Iterable[float], kind: str, w: int, h: int) -> str:
    """Absolute URL of GET /api/imagery/render for these parameters (what other modules embed)."""
    query = urlencode({"collection": collection, "item": item, "bbox": bbox_param(bbox),
                       "kind": kind, "w": w, "h": h})
    return f"{get_settings().public_base_url}/api/imagery/render?{query}"


def _image_response(out: pcdata.Rendered, key: str) -> Response:
    headers = {
        "Cache-Control": IMMUTABLE,
        "ETag": f'"{key}"',
        # Provenance travels in headers, since an image has no JSON body to carry `sources`.
        "X-Imagery-Source": out.source_key,
        "X-Imagery-Collection": out.collection,
        "X-Imagery-Item": out.item,
        "X-Fetch-Id": "" if out.fetch_id is None else str(out.fetch_id),
        "X-Rendered-At": out.rendered_at.isoformat().replace("+00:00", "Z"),
    }
    if out.note:
        headers["X-Imagery-Note"] = out.note.encode("ascii", "replace").decode()
    return Response(content=out.data, media_type=out.media_type, headers=headers)


def _render_or_raise(req: pcdata.RenderRequest, min_valid: float) -> pcdata.Rendered:
    try:
        return pcdata.render(req, min_valid_fraction=min_valid)
    except pcdata.RenderBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from None
    except pcdata.RenderError as exc:
        status = 502 if exc.retryable else 404
        raise HTTPException(status, f"could not render {req.item}: {exc}") from None


@router.get(
    "/render",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}}, "description": "the picture"}},
)
def render_image(
    collection: Annotated[Literal["sentinel-2-l2a", "sentinel-2-c1-l2a"], Query()],
    item: Annotated[str, Query(min_length=20, max_length=80)],
    bbox: Annotated[str, Query(description="minLon,minLat,maxLon,maxLat")],
    kind: Annotated[Literal["truecolor", "swir", "nbr"], Query()] = "truecolor",
    w: Annotated[int, Query(ge=16, le=MAX_SIDE)] = 1024,
    h: Annotated[int, Query(ge=16, le=MAX_SIDE)] = 640,
    fmt: Annotated[Literal["jpg", "png"] | None, Query(alias="format")] = None,
) -> Response:
    """A Sentinel-2 picture of `item` over `bbox`: true colour, SWIR false colour (B12/B8A/B04:
    burnt fields dark, green crops green) or NBR on a red-yellow-green scale.

    JPEG by default for truecolor/swir, PNG for nbr. Cached on disk and immutable once rendered.
    """
    if pcdata.collection_for(item) != collection:
        raise HTTPException(400, f"item {item!r} is not a {collection} item id")
    if not bbox.strip():
        raise HTTPException(400, "bbox is required: minLon,minLat,maxLon,maxLat")
    try:
        b = _round_bbox(parse_bbox(bbox))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not _inside_watch_area(b):
        raise HTTPException(400, f"bbox must lie within the area we watch {AOI_BBOX} (+{AOI_MARGIN_DEG} deg)")
    if (area := _bbox_km2(b)) > MAX_BBOX_KM2:
        raise HTTPException(400, f"bbox covers {area:.0f} km2; the limit is {MAX_BBOX_KM2:.0f} km2")
    if w * h > MAX_PIXELS:
        raise HTTPException(400, f"{w}x{h} is {w * h} pixels; the limit is {MAX_PIXELS}")
    req = pcdata.RenderRequest(collection, item, kind, w, h, fmt or pcdata.default_format(kind), bounds=b)
    out = _render_or_raise(req, CROP_MIN_VALID)
    return _image_response(out, req.cache_key())


@router.get(
    "/tiles/{kind}/{item}/{z}/{x}/{y}",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}, "description": "a 256 px map tile"}},
)
def tile(
    kind: Literal["truecolor", "swir", "nbr"],
    item: Annotated[str, PathParam(min_length=20, max_length=80)],
    z: Annotated[int, PathParam(ge=MIN_TILE_Z, le=MAX_TILE_Z)],
    x: Annotated[int, PathParam(ge=0)],
    y: Annotated[str, PathParam(description="tile row, optionally with .png")],
) -> Response:
    """One 256 px Web Mercator tile of a single Sentinel-2 item, for a map layer.

    PNG with transparency outside the scene. Tiles outside the item's footprint are a 404 without
    any upstream call, so panning around the edge of a scene costs nothing.
    """
    collection = pcdata.collection_for(item)
    if collection is None:
        raise HTTPException(400, f"{item!r} is not a Sentinel-2 L2A item id we serve")
    try:
        row = int(y.removesuffix(".png"))
    except ValueError:
        raise HTTPException(400, "y must be an integer tile row") from None
    if not (x < 2**z and 0 <= row < 2**z):
        raise HTTPException(400, f"tile {z}/{x}/{row} does not exist")
    lonlat = pcdata.tile_lonlat_bounds(z, x, row)
    if not _overlaps_watch_area(lonlat):
        raise HTTPException(404, "tile outside the area we watch")
    try:
        footprint = shape(pcdata.get_item(collection, item)["geometry"])
    except FetchError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"no {collection} item {item}") from None
        raise
    if not footprint.intersects(box(*lonlat)):
        raise HTTPException(404, "tile outside the scene")
    req = pcdata.RenderRequest(collection, item, kind, 256, 256, "png", tile=(z, x, row))
    out = _render_or_raise(req, 0.0)
    return _image_response(out, req.cache_key())


@router.get("/tilejson/{kind}/{item}")
def tilejson(
    session: SessionDep,
    kind: Literal["truecolor", "swir", "nbr"],
    item: Annotated[str, PathParam(min_length=20, max_length=80)],
) -> ORJSONResponse:
    """TileJSON 3.0 for one item's tile layer, so a map can add it with one URL."""
    collection = pcdata.collection_for(item)
    if collection is None:
        raise HTTPException(400, f"{item!r} is not a Sentinel-2 L2A item id we serve")
    try:
        stac = pcdata.get_item(collection, item)
    except FetchError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"no {collection} item {item}") from None
        raise
    west, south, east, north = shape(stac["geometry"]).bounds
    base = get_settings().public_base_url
    payload = {
        "tilejson": "3.0.0",
        "name": f"Sentinel-2 {kind} {item}",
        "scheme": "xyz",
        "tiles": [f"{base}/api/imagery/tiles/{kind}/{item}/{{z}}/{{x}}/{{y}}"],
        "minzoom": MIN_TILE_Z,
        "maxzoom": MAX_TILE_Z,
        "bounds": [west, south, east, north],
        "attribution": "Contains modified Copernicus Sentinel data 2026",
        "datetime": stac.get("properties", {}).get("datetime"),
    }
    key = "stac_pc" if collection == pcdata.PC_S2 else "stac_e84"
    return respond(payload, latest_refs(session, [key]), max_age=3600)


@router.get("/scenes")
def scenes(
    session: SessionDep,
    lon: Annotated[float, Query(ge=-180, le=180)],
    lat: Annotated[float, Query(ge=-90, le=90)],
    days: Annotated[int, Query(ge=1, le=120)] = 30,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ORJSONResponse:
    """Sentinel-2 passes over a point (newest first), each with ready-to-use tile URL templates, so
    the map can show a real scene as a layer. Read from the sentinel module's scene catalogue
    (s2_scene); `usable` is false for passes it skipped as cloudy or failed as implausible."""
    sources = latest_refs(session, ["stac_e84", "stac_pc"])
    if not _table_exists(session, "s2_scene"):
        return respond({"scenes": [], "reason": "the scene catalogue (s2_scene) does not exist yet"}, sources)
    rows = session.execute(text("""
        SELECT item_id, collection, datetime, mgrs_tile, platform, cloud_pct, nodata_pct, status, error
        FROM s2_scene
        WHERE geom IS NOT NULL AND ST_Intersects(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
          AND datetime >= :since
        ORDER BY datetime DESC LIMIT :limit
    """), {"lon": lon, "lat": lat, "since": utcnow() - timedelta(days=days), "limit": limit}).mappings().all()
    base = get_settings().public_base_url
    out = []
    for r in rows:
        item = r["item_id"]
        out.append({
            "date": r["datetime"].date().isoformat(), "datetime": r["datetime"], "item_id": item,
            "collection": r["collection"], "mgrs_tile": r["mgrs_tile"], "platform": r["platform"],
            "tile_cloud_pct": r["cloud_pct"], "nodata_pct": r["nodata_pct"], "status": r["status"],
            "note": r["error"], "usable": r["status"] not in ("skipped", "failed"),
            "tiles": {k: f"{base}/api/imagery/tiles/{k}/{item}/{{z}}/{{x}}/{{y}}" for k in pcdata.KINDS},
            "tilejson": {k: f"{base}/api/imagery/tilejson/{k}/{item}" for k in pcdata.KINDS},
        })
    reason = None if out else f"no Sentinel-2 pass over this point in the last {days} days is catalogued"
    return respond({"scenes": out, "reason": reason}, sources, max_age=300)


# --- featured frames ------------------------------------------------------------------------------

FEATURED_W, FEATURED_H = 1600, 1000
# 6.4 x 4 km has the frames' 1.6 aspect ratio, so each pixel is a 4 m square on the ground.
WINDOW_KM = (6.4, 4.0)
GRID_KM = 0.2  # the window slides in 200 m steps
FRAMES_LOOKBACK_DAYS = 20  # "since 2026-10-10" on 30 Oct: ~4 revisits of an orbit (every 5 days)
FIRMS_LOOKBACK_DAYS = 10
BURN_LOOKBACK_DAYS = FRAMES_LOOKBACK_DAYS  # burns that appear within the frames
MAX_FRAMES = 4
FEATURED_TTL = timedelta(hours=6)
MAX_WINDOW_CLOUD = 0.10  # cloud + shadow + cirrus share of the window (Scene Classification Layer)
MAX_WINDOW_NODATA = 0.01  # the pass must cover the whole window
# A frame on the landing page should look clean: up to 10% cloud is fine for analysis, but a cloud
# and its shadow in a corner read as "burn scar" at a glance. Prefer passes under 5% when there are
# enough of them (seen on 30 Oct 2026: a 9%-cloud pass beat a 0%-cloud one two days older).
PRETTY_WINDOW_CLOUD = 0.05
# Smoke haze raises blue reflectance; sentinel2-burnscars.md section 3.8 flags a pass whose B02
# median rises more than 0.03 above the clear passes around it.
HAZE_RISE = 0.03
FRAME_MIN_VALID = 0.98


@dataclass(frozen=True)
class Window:
    lon: float
    lat: float
    bbox: tuple[float, float, float, float]
    basis: str  # "burned_area" (burn_cluster) or "fire_detections" (FIRMS)
    value: float  # hectares burned, or detections, inside the window
    count: int  # clusters or detections inside the window
    since: datetime
    until: datetime


def _table_exists(session: Session, name: str) -> bool:
    return session.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}).scalar_one()


# Indian Punjab only. The AOI bbox also holds Pakistan's Punjab, which had ~5x more detections than
# Indian Punjab on 29-30 Oct (SYNTHESIS section 1 #1b), so a box must never stand in for the state.
_PUNJAB_SQL = """
    SELECT ST_Union(geom) FROM boundary
    WHERE level = 'state' AND (code = 'IN-PB' OR (name ILIKE 'punjab' AND coalesce(code, '') NOT ILIKE 'PK%'))
"""


def _window_points(session: Session, now: datetime) -> tuple[str, np.ndarray, datetime, str | None]:
    """Weighted points to slide the window over: burn-scar centroids (weight = hectares) from this
    period, or, while there are none, FIRMS detections (weight 1) of the last 10 days. Both are
    clipped to the Indian Punjab polygon. Returns (basis, [[lon, lat, w], ...], since, reason)."""
    if not _table_exists(session, "boundary") or session.execute(text(_PUNJAB_SQL)).scalar() is None:
        return "none", np.empty((0, 3)), now, "the Punjab state boundary is not loaded yet"
    if _table_exists(session, "burn_cluster"):
        since = now - timedelta(days=BURN_LOOKBACK_DAYS)
        rows = session.execute(text(f"""
            WITH pb AS ({_PUNJAB_SQL})
            SELECT ST_X(p), ST_Y(p), area_ha FROM (
                SELECT coalesce(b.centroid, ST_PointOnSurface(b.geom)) AS p, b.area_ha
                FROM burn_cluster b WHERE b.burn_before >= :since AND b.area_ha > 0
            ) c, pb WHERE ST_Intersects(pb.st_union, c.p)
        """), {"since": since}).all()
        if rows:
            return "burned_area", np.array(rows, dtype=float), since, None
    if _table_exists(session, "fire_detection"):
        since = now - timedelta(days=FIRMS_LOOKBACK_DAYS)
        # Spots hot on 3+ nights are kilns or industry (the fires module's static_source flag).
        static = "AND NOT f.static_source" if session.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'fire_detection' AND column_name = 'static_source'")).first() else ""
        rows = session.execute(text(f"""
            WITH pb AS ({_PUNJAB_SQL})
            SELECT f.lon, f.lat, 1.0 FROM fire_detection f, pb
            WHERE f.acq_utc >= :since AND ST_Intersects(pb.st_union, f.geom) {static}
        """), {"since": since}).all()
        if rows:
            return "fire_detections", np.array(rows, dtype=float), since, None
        return "none", np.empty((0, 3)), since, "no burn scars and no FIRMS detections in Punjab yet"
    return "none", np.empty((0, 3)), now, "neither burn_cluster nor fire_detection exists yet"


def densest_window(points: np.ndarray, size_km: tuple[float, float] = WINDOW_KM,
                   cell_km: float = GRID_KM) -> tuple[float, float, float, int]:
    """The heaviest position of a size_km box slid over weighted points:
    (lon, lat, weight inside, points inside)."""
    return window_candidates(points, size_km, cell_km, n=1)[0]


def window_candidates(points: np.ndarray, size_km: tuple[float, float] = WINDOW_KM,
                      cell_km: float = GRID_KM, n: int = 300) -> list[tuple[float, float, float, int]]:
    """The `n` heaviest positions of a size_km box slid over weighted points, heaviest first, each as
    (lon, lat, weight inside, points inside).

    How: bin the points on a 200 m grid (local kilometres), then an integral image gives the sum
    under every box position at once (one cumulative sum per axis); sorting those sums ranks every
    position. More than one is returned so the caller can skip positions that fail a test (such as
    spilling over the border).
    """
    lon, lat, w = points[:, 0], points[:, 1], points[:, 2]
    lat0 = float(np.mean(lat))
    kx, ky = _km_per_deg_lon(lat0), KM_PER_DEG
    bx, by = round(size_km[0] / cell_km), round(size_km[1] / cell_km)
    # A margin of one window on every side lets the box centre sit on any point, edges included.
    x = (lon - lon.min()) * kx + bx * cell_km
    y = (lat - lat.min()) * ky + by * cell_km
    nx, ny = int(x.max() / cell_km) + 2 * bx + 1, int(y.max() / cell_km) + 2 * by + 1
    grid, _, _ = np.histogram2d(y, x, bins=(ny, nx), range=((0, ny * cell_km), (0, nx * cell_km)), weights=w)
    ii = np.zeros((ny + 1, nx + 1))
    ii[1:, 1:] = grid.cumsum(0).cumsum(1)
    sums = ii[by:, bx:] - ii[:-by, bx:] - ii[by:, :-bx] + ii[:-by, :-bx]
    out = []
    for flat in np.argsort(sums, axis=None)[::-1][:n]:
        iy, ix = np.unravel_index(int(flat), sums.shape)
        cx = (ix + bx / 2) * cell_km  # box centre in the same local kilometres as x and y
        cy = (iy + by / 2) * cell_km
        clon = lon.min() + (cx - bx * cell_km) / kx
        clat = lat.min() + (cy - by * cell_km) / ky
        inside = (np.abs(x - cx) <= size_km[0] / 2) & (np.abs(y - cy) <= size_km[1] / 2)
        out.append((float(clon), float(clat), float(w[inside].sum()), int(inside.sum())))
    return out


def window_bbox(lon: float, lat: float, size_km: tuple[float, float] = WINDOW_KM) -> tuple[float, float, float, float]:
    dlon = size_km[0] / 2 / _km_per_deg_lon(lat)
    dlat = size_km[1] / 2 / KM_PER_DEG
    return _round_bbox((lon - dlon, lat - dlat, lon + dlon, lat + dlat))


MIN_INSIDE_INDIA = 0.98  # the frames should show Indian fields: Tarn Taran's hotspots touch the border


def select_window(session: Session, now: datetime | None = None) -> tuple[Window | None, str | None]:
    """The featured window, chosen from real data, or (None, why not).

    The heaviest position whose box lies at least 98% inside Indian Punjab: the densest burning on
    30 Oct 2026 was near Wan (Tarn Taran), where a box centred on the fires reached 8% into
    Pakistan."""
    now = now or utcnow()
    basis, pts, since, reason = _window_points(session, now)
    if basis == "none" or len(pts) == 0:
        return None, reason
    from shapely import from_wkb, prepare

    punjab = from_wkb(bytes(session.execute(text(f"SELECT ST_AsBinary(({_PUNJAB_SQL}))")).scalar_one()))
    prepare(punjab)
    for lon, lat, value, count in window_candidates(pts):
        b = window_bbox(lon, lat)
        win = box(*b)
        if punjab.intersection(win).area / win.area >= MIN_INSIDE_INDIA:
            return Window(lon, lat, b, basis, round(value, 2), count, since, now), None
    return None, "no burning hotspot window fits inside Indian Punjab"


@dataclass
class Pass:
    """One satellite pass over the window, with the item we will draw it from."""

    day: str
    platform: str
    collection: str
    item: dict[str, Any]
    tile_cloud_pct: float | None
    stats: pcdata.AreaStats | None = None
    hazy: bool = False


def _coverage(item: dict[str, Any], win: Any) -> float:
    try:
        return float(shape(item["geometry"]).intersection(win).area / win.area)
    except Exception:
        return 0.0


def _passes(polygon: dict[str, Any], since: datetime, until: datetime) -> tuple[list[Pass], list[int]]:
    """Every pass that fully covers the window, in both catalogues, one item per pass.

    PC's item is preferred (its tiler renders fast) when its pixels pass the gate; otherwise the
    same pass from Earth Search (which also has passes PC has not ingested yet)."""
    win = shape(polygon)
    fetch_ids: list[int] = []
    by_pass: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for collection in (pcdata.PC_S2, pcdata.E84_S2):
        try:
            found = pcdata.search(collection, polygon, since, until)
        except FetchError as exc:  # one catalogue down: carry on with the other
            log.warning("featured: %s search failed: %s", collection, exc)
            continue
        fetch_ids += found.fetch_ids[:1]
        for item in found.items:
            sid = pcdata.scene_id(item["id"])
            if sid is None or _coverage(item, win) < 1 - MAX_WINDOW_NODATA:
                continue
            slot = by_pass.setdefault((sid.day.isoformat(), sid.platform), {})
            slot.setdefault(collection, item)
    out = []
    for (day, platform), items in sorted(by_pass.items()):
        pc_item = items.get(pcdata.PC_S2)
        chosen: tuple[str, dict[str, Any]] | None = None
        if pc_item is not None:
            try:
                if pcdata.pc_gate(pc_item["id"]).ok:
                    chosen = (pcdata.PC_S2, pc_item)
            except FetchError as exc:
                log.warning("featured: gate for %s failed: %s", pc_item["id"], exc)
        if chosen is None and pcdata.E84_S2 in items:
            chosen = (pcdata.E84_S2, items[pcdata.E84_S2])
        if chosen is None:
            continue
        cloud = chosen[1]["properties"].get("eo:cloud_cover")
        out.append(Pass(day, platform, chosen[0], chosen[1], None if cloud is None else float(cloud)))
    return out, fetch_ids


def _measure(p: Pass, polygon: dict[str, Any], bounds: tuple[float, float, float, float]) -> Pass:
    try:
        if p.collection == pcdata.PC_S2:
            p.stats = pcdata.pc_area_stats(p.item["id"], polygon)
        else:
            p.stats = pcdata.e84_area_stats(p.item, bounds)
    except Exception as exc:  # unmeasured passes are left out, never guessed clear
        log.warning("featured: could not measure %s: %s", p.item["id"], exc)
    return p


def _cloud(p: Pass) -> float:
    return p.stats.cloud_fraction if p.stats else 1.0


def _choose(clear: list[Pass]) -> list[Pass]:
    """Up to MAX_FRAMES passes, oldest to newest, spread over the period (the change is the story).

    The pool is the clean passes (under PRETTY_WINDOW_CLOUD, no smoke haze), topped up with the
    clearest others only if there are fewer than MAX_FRAMES clean ones. From the pool: the oldest
    and the newest, then for each evenly spaced moment between them the nearest unused pass."""
    clean = [p for p in clear if _cloud(p) <= PRETTY_WINDOW_CLOUD and not p.hazy]
    if len(clean) < MAX_FRAMES:
        taken = {id(p) for p in clean}
        others = sorted((p for p in clear if id(p) not in taken), key=lambda p: (p.hazy, _cloud(p)))
        clean = clean + others[: MAX_FRAMES - len(clean)]
    pool = sorted(clean, key=lambda p: p.day)
    if len(pool) <= MAX_FRAMES:
        return pool
    days = [date.fromisoformat(p.day).toordinal() for p in pool]
    keep = {0, len(pool) - 1}
    for j in range(1, MAX_FRAMES - 1):
        target = days[0] + (days[-1] - days[0]) * j / (MAX_FRAMES - 1)
        free = [i for i in range(len(pool)) if i not in keep]
        keep.add(min(free, key=lambda i: (abs(days[i] - target), _cloud(pool[i]))))
    return [pool[i] for i in sorted(keep)]


def _place_name(session: Session, lon: float, lat: float) -> tuple[str, str | None, str | None]:
    """'Near <village>, <district>' when places and boundaries are loaded; never a guessed name."""
    admin = district_for_point(session, lon, lat)
    district = admin.district if admin else None
    state = admin.state if admin else None
    near = nearest_place(session, lon, lat, kinds=("village", "town"), max_km=5.0)
    if near and district:
        return f"Near {near.name}, {district} district", district, state
    if near:
        return f"Near {near.name}", near.district, state or near.state
    if district:
        return f"{district} district, Punjab", district, state
    return f"Punjab, {lat:.3f}N {lon:.3f}E", district, state


def build_featured(session: Session, window: Window | None = None) -> dict[str, Any]:
    """Choose the window (unless one is given), find its clear passes, render and check every
    frame. Slow (20-60 s): run by the `featured` job and, only when nothing is cached yet, by the
    first request."""
    now = utcnow()
    reason = None
    if window is None:
        window, reason = select_window(session, now)
    if window is None:
        return {"aoi": None, "frames": [], "reason": reason, "built_at": now, "sources": []}
    b = window.bbox
    polygon = mapping(box(*b))
    since = now - timedelta(days=FRAMES_LOOKBACK_DAYS)
    passes, search_ids = _passes(polygon, since, now)
    with ThreadPoolExecutor(4) as pool:
        passes = list(pool.map(lambda p: _measure(p, polygon, b), passes))
    clear = [
        p for p in passes
        if p.stats is not None and p.stats.nodata_fraction <= MAX_WINDOW_NODATA
        and p.stats.cloud_fraction <= MAX_WINDOW_CLOUD
    ]
    blues = [p.stats.blue_median for p in clear if p.stats and p.stats.blue_median is not None]
    if blues:
        typical = float(np.median(blues))
        for p in clear:
            if p.stats and p.stats.blue_median is not None:
                p.hazy = p.stats.blue_median - typical > HAZE_RISE
    # The preferred passes first; the other clear passes stand by in case one fails to render.
    preferred = _choose(clear)
    chosen_ids = {id(p) for p in preferred}
    backups = sorted((p for p in clear if id(p) not in chosen_ids),
                     key=lambda p: (p.hazy, p.stats.cloud_fraction if p.stats else 1.0))
    frames: list[tuple[Pass, list[int]]] = []
    render_ids: list[int] = []
    dropped: list[dict[str, str]] = []
    for p in preferred + backups:
        if len(frames) == MAX_FRAMES:
            break
        ok, ids, why = _render_frame(p, b)
        if ok:
            frames.append((p, ids))
            render_ids += ids
        else:
            dropped.append({"item": p.item["id"], "reason": why})
    frames.sort(key=lambda fp: fp[0].day)
    name, district, state = _place_name(session, window.lon, window.lat)
    payload = {
        "aoi": {
            "name": name,
            "bbox": list(b),
            "center": [round(window.lon, 6), round(window.lat, 6)],
            "size_km": list(WINDOW_KM),
            "district": district,
            "state": state,
            "chosen_by": window.basis,
            "chosen_value": window.value,
            "chosen_unit": "ha" if window.basis == "burned_area" else "detections",
            "chosen_count": window.count,
            "chosen_since": window.since,
        },
        "frames": [_frame_json(p, b) for p, _ in frames],
        "passes_considered": len(passes),
        "passes_clear": len(clear),
        "dropped": dropped,
        "reason": None if frames else "no clear Sentinel-2 pass over the window could be rendered",
        "built_at": now,
    }
    basis_key = "firms" if window.basis == "fire_detections" else "stac_e84"
    refs = latest_refs(session, [basis_key])
    for fid in dict.fromkeys(search_ids + render_ids):
        ref = ref_for_fetch(session, fid)
        if ref is not None:
            refs.append(ref)
    payload["sources"] = [r.model_dump(mode="json") for r in refs]
    return payload


def _render_frame(p: Pass, b: tuple[float, float, float, float]) -> tuple[bool, list[int], str]:
    """Render both kinds of a frame through the same path as GET /render (filling its cache)."""
    ids = []
    for kind in ("truecolor", "swir"):
        req = pcdata.RenderRequest(p.collection, p.item["id"], kind, FEATURED_W, FEATURED_H, "jpg", bounds=b)
        try:
            out = pcdata.render(req, min_valid_fraction=FRAME_MIN_VALID)
        except Exception as exc:  # RenderError, FetchError, SourceUnavailable
            return False, ids, f"{kind}: {exc}"[:300]
        if out.fetch_id is not None:
            ids.append(out.fetch_id)
    return True, ids, "ok"


def _frame_json(p: Pass, b: tuple[float, float, float, float]) -> dict[str, Any]:
    s = p.stats
    return {
        "date": p.day,
        "datetime": p.item["properties"].get("datetime"),
        "item_id": p.item["id"],
        "collection": p.collection,
        "platform": p.platform,
        "cloud_pct": None if s is None else round(100 * s.cloud_fraction, 2),
        "tile_cloud_pct": p.tile_cloud_pct,
        "hazy": p.hazy,
        "blue_median": None if s is None or s.blue_median is None else round(s.blue_median, 4),
        "truecolor_url": render_url(p.collection, p.item["id"], b, "truecolor", FEATURED_W, FEATURED_H),
        "swir_url": render_url(p.collection, p.item["id"], b, "swir", FEATURED_W, FEATURED_H),
        "width": FEATURED_W,
        "height": FEATURED_H,
    }


# The built payload lives on disk, so it survives restarts and the API and the job process share it.
def _featured_path() -> Path:
    return get_settings().cache_path("imagery", "featured.json")


def load_featured() -> dict[str, Any] | None:
    try:
        return orjson.loads(_featured_path().read_bytes())
    except (OSError, ValueError):
        return None


def _store_featured(payload: dict[str, Any]) -> None:
    path = _featured_path()
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_bytes(orjson.dumps(payload, option=orjson.OPT_UTC_Z | orjson.OPT_NAIVE_UTC, default=str))
    tmp.replace(path)


_build_lock = threading.Lock()  # one build at a time per process


def _age(payload: dict[str, Any]) -> timedelta:
    try:
        built = datetime.fromisoformat(str(payload["built_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return timedelta.max
    return utcnow() - built


def refresh_featured(*, force: bool = False) -> dict[str, Any]:
    """Rebuild the featured frames when they are older than 6 h, the window has moved, or `force`.

    Used by the `featured` job. Cheap when nothing changed: choosing the window is one query.
    A build that finds no frames never replaces frames that worked."""
    with _build_lock, session_scope() as session:
        current = load_featured()
        window, reason = select_window(session)
        if window is None:
            return {"skipped": reason}
        fresh = current is not None and _age(current) < FEATURED_TTL and current.get("frames")
        same = fresh and _same_window(current, window)
        if same and not force:
            return {"skipped": "frames are fresh and the window has not moved",
                    "built_at": current.get("built_at")}
        payload = build_featured(session)
    if payload["frames"] or not (current and current.get("frames")):
        _store_featured(payload)
    return {"frames": len(payload["frames"]), "window": payload["aoi"] and payload["aoi"]["name"],
            "basis": payload["aoi"] and payload["aoi"]["chosen_by"], "reason": payload.get("reason"),
            "dropped": len(payload.get("dropped", []))}


def _same_window(current: dict[str, Any], window: Window) -> bool:
    aoi = current.get("aoi") or {}
    center = aoi.get("center")
    if not center or aoi.get("chosen_by") != window.basis:
        return False
    dx = (center[0] - window.lon) * _km_per_deg_lon(window.lat)
    dy = (center[1] - window.lat) * KM_PER_DEG
    return math.hypot(dx, dy) < 1.0  # within a kilometre: the same fields, keep the frames


def _refresh_quietly() -> None:
    try:
        refresh_featured()
    except Exception:
        log.exception("background refresh of the featured frames failed")


@router.get("/featured")
def featured() -> ORJSONResponse:
    """The landing page's crossfade: frames of one 6.4 x 4 km window in Indian Punjab, chosen
    automatically from the data (most burned area in burn scars, or most FIRMS detections while
    there are no scars yet), one per clear Sentinel-2 pass of the last 20 days, oldest first.

    Served from a cache rebuilt every 6 h by the `featured` job. When the cache is older than that,
    the stale frames are served (marked `stale`) while a rebuild runs in the background.
    """
    payload = load_featured()
    if payload is None:
        # Nothing built yet (the job has not run): build now, once, for everyone waiting.
        if _build_lock.acquire(timeout=180):
            try:
                payload = load_featured()
                if payload is None:
                    with session_scope() as session:
                        payload = build_featured(session)
                    _store_featured(payload)
                    payload = load_featured()
            except Exception as exc:  # an upstream or database failure: say so, never fake frames
                log.exception("building the featured frames failed")
                raise HTTPException(503, f"the featured frames could not be built: {type(exc).__name__}: "
                                         f"{exc}"[:400], headers={"Retry-After": "120"}) from None
            finally:
                _build_lock.release()
        if payload is None:
            raise HTTPException(503, "the featured frames are being built; try again in a minute",
                                headers={"Retry-After": "60"})
    stale = _age(payload) > FEATURED_TTL
    if stale and not _build_lock.locked():
        threading.Thread(target=_refresh_quietly, name="featured-refresh", daemon=True).start()
    sources = [SourceRef(**s) for s in payload.get("sources", [])]
    body = {k: v for k, v in payload.items() if k != "sources"}
    body["stale"] = stale
    return respond(body, sources, max_age=300)


__all__ = ["build_featured", "densest_window", "refresh_featured", "render_url", "router",
           "select_window"]
