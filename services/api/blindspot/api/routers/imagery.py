"""Imagery: Sentinel-2 pictures (crops and map tiles) and the landing page's featured frames.

GET /api/imagery/render                         one picture of an item over a bbox
GET /api/imagery/tiles/{kind}/{item}/{z}/{x}/{y}  the same recipes as XYZ map tiles (contract addition)
GET /api/imagery/tilejson/{kind}/{item}          TileJSON for that tile layer (contract addition)
GET /api/imagery/featured                        the 6.4 x 4 km window of Indian Punjab with the most
                                                 burning, and its clear passes of the last 20 days

Pictures come from blindspot.sources.pcdata: the Planetary Computer tiler, or our own renderer when
PC's pixels fail the plausibility gate. Every picture is checked before it is served and kept on disk
forever, so the routes send immutable cache headers. The featured frames, the case-file chips and the
featured manifest are also kept in the database (rendered_asset, blindspot.models.assets), because a
serverless instance starts with an empty disk: the landing page must never wait on a live render. A failure is always a JSON error, never a
placeholder image. Requests are bounded (area, pixels, collections, the area we watch) so the proxy
cannot be used to fetch arbitrary imagery at our expense.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import numpy as np
import orjson
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response
from shapely.geometry import box, mapping, shape
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.api import SessionDep, respond
from blindspot.api.cache import IMMUTABLE  # one year, for browsers and the CDN alike
from blindspot.api.responses import ORJSONResponse
from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.geo import AOI_BBOX, district_for_point, nearest_place, parse_bbox
from blindspot.http import FetchError
from blindspot.models.client_quota import (
    client_address,
    hit_or_allow,
    on_vercel,
    shared_limits_enabled,
)
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch, utcnow
from blindspot.sources import pcdata

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/imagery", tags=["imagery"])

MAX_SIDE = 2048
MAX_PIXELS = 2048 * 1024  # 2.1 MP: enough for the 1600 x 1000 landing frames
MAX_BBOX_KM2 = 900.0  # a 30 x 30 km crop; scar chips are ~1 km, the featured window 25.6 km2
AOI_MARGIN_DEG = 0.25  # requests must stay within the area we watch, plus this margin
MIN_TILE_Z, MAX_TILE_Z = 9, 16  # z9 ~ 300 m/px; Sentinel-2's 10 m is about z14, z15-16 overzoom
CROP_MIN_VALID = 0.5  # half of a requested crop must have data (a scene edge may cut a chip)
KM_PER_DEG = 111.32  # length of one degree of latitude (and of longitude at the equator), km
# A crop's corners snap to a power-of-two grid of about 1/SNAP_CELLS of its longer side (each edge
# moves at most 1/64 of the side, under 2%), so requests a few metres apart share one cache entry
# instead of forcing a fresh 1-20 s render each. The featured frames and the case-file chips keep
# their exact boxes (their URLs are stored and cached as immutable everywhere).
SNAP_CELLS = 32
# A box must be at least this long on its longer side (about 11 m, one Sentinel-2 pixel): a smaller
# one has nothing to show, and its snapped corners would collapse at 6 decimals.
MIN_BBOX_SIDE_DEG = 0.0001
# How far (deg) a case-file chip's centre may lie from its scar's extent centre: the chip box is that
# centre +- half a side, rounded to 6 decimals, so the true offset is under 1e-6; 1e-4 (~10 m) leaves
# room for float rounding and still rules out every other cluster.
CHIP_CENTRE_TOL = 1e-4
# Live renders (a picture not yet drawn) per client. Pictures already drawn (disk, rendered_asset)
# never count. A case file outside the stored ones costs 6 renders (pre/post x 3 kinds).
RENDER_PER_MINUTE = int(os.environ.get("IMAGERY_RENDER_PER_MINUTE", "30"))
RENDER_PER_DAY = int(os.environ.get("IMAGERY_RENDER_PER_DAY", "300"))
# Live map tiles (a tile not yet on this instance's disk) per client, a separate budget: one map
# view of a scene loads 30-60 tiles, and people behind one NAT (a venue's Wi-Fi) share an address.
TILES_PER_MINUTE = int(os.environ.get("IMAGERY_TILES_PER_MINUTE", "600"))
TILES_PER_DAY = int(os.environ.get("IMAGERY_TILES_PER_DAY", "6000"))


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


def snap_bbox(b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """`b` with every corner on the nearest point of a grid whose step is the power of two (in
    degrees) just under 1/SNAP_CELLS of the box's longer side: a 1 km chip snaps to ~27 m, a 30 km
    crop to ~870 m. The same box always snaps the same way; one already on the grid is unchanged."""
    side = max(b[2] - b[0], b[3] - b[1])
    if side <= 0:
        return b
    step = 2.0 ** math.floor(math.log2(side / SNAP_CELLS))
    w, s, e, n = (round(v / step) * step for v in b)
    if e <= w:
        e = w + step
    if n <= s:
        n = s + step
    return _round_bbox((w, s, e, n))


def _require_watched_item(item: str) -> None:
    """Refuse an item whose MGRS tile does not intersect the area we watch, from its id alone, so
    no upstream lookup is ever made for a scene elsewhere (pcdata.watched_tiles derives the tiles
    from AOI_BBOX)."""
    tile = pcdata.item_tile(item)
    if tile is None or tile not in pcdata.watched_tiles(AOI_BBOX, AOI_MARGIN_DEG):
        raise HTTPException(400, f"item {item!r} is on MGRS tile {tile}, which does not intersect the area we "
                                 f"watch {AOI_BBOX} (+{AOI_MARGIN_DEG} deg)")


_render_limiter_instance: Any = None
_tile_limiter_instance: Any = None


def _render_limiter() -> Any:
    global _render_limiter_instance
    if _render_limiter_instance is None:
        from blindspot.agent.runner import RateLimiter

        _render_limiter_instance = RateLimiter(RENDER_PER_MINUTE, RENDER_PER_DAY, noun="new pictures")
    return _render_limiter_instance


def _tile_limiter() -> Any:
    global _tile_limiter_instance
    if _tile_limiter_instance is None:
        from blindspot.agent.runner import RateLimiter

        _tile_limiter_instance = RateLimiter(TILES_PER_MINUTE, TILES_PER_DAY, noun="new map tiles")
    return _tile_limiter_instance


def _live_limiter(scope: str) -> Any:
    return _tile_limiter() if scope == "tiles" else _render_limiter()


def _too_many(scope: str, exc: Any) -> HTTPException:
    """The 429 for a refused live render or tile: the API's JSON error, with Retry-After."""
    wait = int(exc.retry_after_s) + 1
    what = "map tiles" if scope == "tiles" else "pictures"
    return HTTPException(429, f"Too many new {what} from this address ({exc}); {what} already drawn "
                              f"are not limited. Try again in {wait} s.",
                         headers={"Retry-After": str(wait), "Cache-Control": "no-store"})


def _client_of(request: Request) -> str:
    return client_address(request.headers, request.client.host if request.client else None)


def _peek_live(request: Request, scope: str) -> None:
    """429 if this client is already past its limit of live `scope` work on this instance (its own
    count, or a refusal of the shared count it remembers), without counting and without any query.
    Called before a request does database or upstream work it would only need for a live render."""
    from blindspot.agent.runner import RateLimited

    try:
        _live_limiter(scope).peek(_client_of(request))
    except RateLimited as exc:
        raise _too_many(scope, exc) from None


def _count_live(request: Request, scope: str) -> None:
    """Count one live render ("render") or live map tile ("tiles") for this client, or answer 429.

    In memory first; on serverless also in Postgres across instances (blindspot.models.client_quota),
    under a salted hash of the real client address. A shared refusal is remembered on this instance
    until it lapses, so a client past its limit costs no further query here."""
    from blindspot.agent.runner import RateLimited

    address = _client_of(request)
    mem = _live_limiter(scope)
    try:
        mem.hit(address)
        if shared_limits_enabled():
            refusal = hit_or_allow(scope, address, mem.per_minute, mem.per_day, mem.noun)
            if refusal is not None:
                mem.forget(address)
                mem.block(address, refusal.retry_after_s, refusal.reason)
                raise RateLimited(refusal.retry_after_s, refusal.reason)
    except RateLimited as exc:
        raise _too_many(scope, exc) from None


def _count_live_render(request: Request) -> None:
    _count_live(request, "render")


def render_url(collection: str, item: str, bbox: Iterable[float], kind: str, w: int, h: int) -> str:
    """Absolute URL of GET /api/imagery/render for these parameters (what other modules embed)."""
    query = urlencode({"collection": collection, "item": item, "bbox": bbox_param(bbox),
                       "kind": kind, "w": w, "h": h})
    return f"{get_settings().public_base_url}/api/imagery/render?{query}"


def _image_response(out: pcdata.Rendered, key: str,
                    bounds: tuple[float, float, float, float] | None = None) -> Response:
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
    if bounds is not None:  # the box actually drawn (a requested box may have been snapped)
        headers["X-Imagery-Bbox"] = bbox_param(bounds)
    return Response(content=out.data, media_type=out.media_type, headers=headers)


def _render_or_raise(req: pcdata.RenderRequest, min_valid: float, **options: Any) -> pcdata.Rendered:
    try:
        return pcdata.render(req, min_valid_fraction=min_valid, **options)
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
    request: Request,
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

    A picture already drawn for exactly this request (on disk, or stored: the featured frames and
    case-file chips) is served as is. Otherwise the current featured frames and the case-file
    chips are drawn for their exact box; any other box is snapped to a grid (snap_bbox; the box
    drawn is in X-Imagery-Bbox), and a picture not yet drawn counts against the client's limit of
    live renders (429 with Retry-After past it). Items must lie on the MGRS tiles of the area we watch.
    """
    if pcdata.collection_for(item) != collection:
        raise HTTPException(400, f"item {item!r} is not a {collection} item id")
    _require_watched_item(item)
    if not bbox.strip():
        raise HTTPException(400, "bbox is required: minLon,minLat,maxLon,maxLat")
    try:
        b = _round_bbox(parse_bbox(bbox))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not _inside_watch_area(b):
        raise HTTPException(400, f"bbox must lie within the area we watch {AOI_BBOX} (+{AOI_MARGIN_DEG} deg)")
    if b[2] <= b[0] or b[3] <= b[1] or max(b[2] - b[0], b[3] - b[1]) < MIN_BBOX_SIDE_DEG:
        raise HTTPException(400, f"bbox must be at least {MIN_BBOX_SIDE_DEG} deg on its longer side, "
                                 "with min < max at 6 decimals")
    if (area := _bbox_km2(b)) > MAX_BBOX_KM2:
        raise HTTPException(400, f"bbox covers {area:.0f} km2; the limit is {MAX_BBOX_KM2:.0f} km2")
    if w * h > MAX_PIXELS:
        raise HTTPException(400, f"{w}x{h} is {w * h} pixels; the limit is {MAX_PIXELS}")
    req = pcdata.RenderRequest(collection, item, kind, w, h, fmt or pcdata.default_format(kind), bounds=b)
    # 1. Already drawn for exactly this URL: every stored frame and chip is served byte for byte.
    if (hit := pcdata.lookup(req)) is not None:
        return _image_response(hit, req.cache_key(), req.bounds)
    # 2. A current featured frame or a case-file chip keeps its exact box and is stored once drawn
    #    (the next serverless instance reads it back); any other box snaps to the grid. Telling a
    #    chip needs a database query, so a client already past its limit is refused before it.
    if _may_be_case_chip(req):
        _peek_live(request, "render")
    role = _persist_role(req)
    if role is None:
        req = replace(req, bounds=snap_bbox(b))
        if (hit := pcdata.lookup(req, stored=False)) is not None:  # rendered_asset never holds these
            return _image_response(hit, req.cache_key(), req.bounds)
    # 3. A live render: count it for this client first.
    _count_live_render(request)
    out = _render_or_raise(req, CROP_MIN_VALID, persist=role, stored_lookup=False)
    return _image_response(out, req.cache_key(), req.bounds)


def _may_be_case_chip(req: pcdata.RenderRequest) -> bool:
    from blindspot.api.routers import scars

    return req.width == req.height == scars.CHIP_PX and req.bounds is not None


def _persist_role(req: pcdata.RenderRequest) -> str | None:
    """'frame' or 'chip' when a picture just rendered for GET /render belongs in rendered_asset: a
    frame of the current featured window, or a chip of a burn scar's case file. Any other crop
    someone asks for stays on this instance's disk only."""
    try:
        if (req.width, req.height) == (FEATURED_W, FEATURED_H):
            frames = {r.cache_key() for r in _frame_requests(load_featured())}
            return "frame" if req.cache_key() in frames else None
        from blindspot.api.routers import (
            scars,  # the case file's chip geometry is the scars module's
        )

        if req.width == req.height == scars.CHIP_PX and req.bounds is not None:
            with session_scope() as session:
                return "chip" if _is_case_chip(session, req, scars._chip_bbox) else None
    except Exception as exc:  # never fail a picture that was rendered fine
        log.warning("could not tell whether %s is a featured asset: %s", req.item, exc)
    return None


def _is_case_chip(session: Session, req: pcdata.RenderRequest, chip_bbox: Any) -> bool:
    if req.bounds is None or not _table_exists(session, "burn_cluster"):
        return False
    w, s, e, n = req.bounds
    # A chip is centred on its scar's extent (scars._chip_bbox), so only clusters whose extent is
    # centred within CHIP_CENTRE_TOL of the box can match: at most one or two rows come back
    # instead of every cluster of the scene under the box (up to 50 geometries, ~14 KB).
    rows = session.execute(text("""
        SELECT ST_AsGeoJSON(geom)::json, ST_Y(centroid) FROM burn_cluster
        WHERE (scene_pre = :item OR scene_post = :item) AND geom && ST_MakeEnvelope(:w, :s, :e, :n, 4326)
          AND abs((ST_XMin(geom) + ST_XMax(geom)) / 2 - :cx) < :tol
          AND abs((ST_YMin(geom) + ST_YMax(geom)) / 2 - :cy) < :tol
        LIMIT 5
    """), {"item": req.item, "w": w, "s": s, "e": e, "n": n, "cx": (w + e) / 2, "cy": (s + n) / 2,
           "tol": CHIP_CENTRE_TOL}).all()
    return any(_round_bbox(chip_bbox(geom, lat)) == req.bounds for geom, lat in rows if geom and lat is not None)


@router.get(
    "/tiles/{kind}/{item}/{z}/{x}/{y}",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}, "description": "a 256 px map tile"}},
)
def tile(
    request: Request,
    kind: Literal["truecolor", "swir", "nbr"],
    item: Annotated[str, PathParam(min_length=20, max_length=80)],
    z: Annotated[int, PathParam(ge=MIN_TILE_Z, le=MAX_TILE_Z)],
    x: Annotated[int, PathParam(ge=0)],
    y: Annotated[str, PathParam(description="tile row, optionally with .png")],
) -> Response:
    """One 256 px Web Mercator tile of a single Sentinel-2 item, for a map layer.

    PNG with transparency outside the scene. Tiles outside the item's footprint are a 404 without
    any upstream call, so panning around the edge of a scene costs nothing. A tile already drawn on
    this instance is served as is; a live one counts against the client's limit of new map tiles
    (a budget of its own, larger than the live render limit; 429 with Retry-After past it).
    """
    collection = pcdata.collection_for(item)
    if collection is None:
        raise HTTPException(400, f"{item!r} is not a Sentinel-2 L2A item id we serve")
    _require_watched_item(item)
    try:
        row = int(y.removesuffix(".png"))
    except ValueError:
        raise HTTPException(400, "y must be an integer tile row") from None
    if not (x < 2**z and 0 <= row < 2**z):
        raise HTTPException(400, f"tile {z}/{x}/{row} does not exist")
    lonlat = pcdata.tile_lonlat_bounds(z, x, row)
    if not _overlaps_watch_area(lonlat):
        raise HTTPException(404, "tile outside the area we watch")
    req = pcdata.RenderRequest(collection, item, kind, 256, 256, "png", tile=(z, x, row))
    if (hit := pcdata.lookup(req, stored=False)) is not None:  # drawn on this instance already
        return _image_response(hit, req.cache_key())
    _peek_live(request, "tiles")  # past the limit: no STAC lookup either
    try:
        footprint = shape(pcdata.get_item(collection, item)["geometry"])
    except FetchError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"no {collection} item {item}") from None
        raise
    if not footprint.intersects(box(*lonlat)):
        raise HTTPException(404, "tile outside the scene")
    _count_live(request, "tiles")
    out = _render_or_raise(req, 0.0)
    return _image_response(out, req.cache_key())


@router.get("/tilejson/{kind}/{item}")
def tilejson(
    session: SessionDep,
    kind: Literal["truecolor", "swir", "nbr"],
    item: Annotated[str, PathParam(min_length=20, max_length=80)],
) -> ORJSONResponse:
    """TileJSON 3.0 for one item's tile layer, so a map can add it with one URL. Only items on the
    MGRS tiles of the area we watch: any other is refused before the STAC lookup."""
    collection = pcdata.collection_for(item)
    if collection is None:
        raise HTTPException(400, f"{item!r} is not a Sentinel-2 L2A item id we serve")
    _require_watched_item(item)
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
FEATURED_TTL = timedelta(hours=6)  # the `featured` job rebuilds frames older than this
# When the served frames count as stale. A long-lived local process runs the job itself every 6 h
# (jobs/risk.py, every_minutes=360), so there it is FEATURED_TTL. On Vercel the frames come only
# from GitHub Actions, where `featured` runs once a day, chained after `sentinel` at 11:40 UTC
# (.github/workflows/data-jobs.yml), and that chain may run up to its 150 min timeout: one day plus
# the chain plus 3.5 h of slack for a slow runner is 30 h.
FEATURED_JOB_EVERY_SERVERLESS = timedelta(days=1)
FEATURED_STALE_AFTER_SERVERLESS = FEATURED_JOB_EVERY_SERVERLESS + timedelta(minutes=150) + timedelta(hours=3.5)
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
            out = pcdata.render(req, min_valid_fraction=FRAME_MIN_VALID, persist="frame")
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


# The built payload lives on disk and in rendered_asset: the disk copy survives restarts on one
# machine, the database copy reaches every serverless instance and every job runner. A disk copy
# older than MANIFEST_RECHECK is compared with the database's, which the `featured` job keeps current.
FEATURED_KEY = "featured.json"
MANIFEST_RECHECK = timedelta(minutes=5)


def _featured_path() -> Path:
    return get_settings().cache_path("imagery", FEATURED_KEY)


def _write_featured_file(data: bytes) -> None:
    path = _featured_path()
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def load_featured() -> dict[str, Any] | None:
    path = _featured_path()
    disk: dict[str, Any] | None = None
    try:
        disk = orjson.loads(path.read_bytes())
        if utcnow().timestamp() - path.stat().st_mtime < MANIFEST_RECHECK.total_seconds():
            return disk
    except (OSError, ValueError):
        pass
    stored = _load_stored_manifest()
    if stored is None or (disk is not None and _age(disk) <= _age(stored[0])):
        if disk is not None:
            with contextlib.suppress(OSError):
                os.utime(path)  # checked: trust the disk copy for another MANIFEST_RECHECK
        return disk
    _write_featured_file(stored[1])
    return stored[0]


def _load_stored_manifest() -> tuple[dict[str, Any], bytes] | None:
    from blindspot.models.assets import load_asset

    try:
        row = load_asset(FEATURED_KEY)
        return None if row is None else (orjson.loads(row["data"]), row["data"])
    except Exception as exc:  # no database or no table yet: the disk copy (if any) is all there is
        log.warning("could not read the stored featured manifest: %s", exc)
        return None


def _store_featured(payload: dict[str, Any]) -> None:
    from blindspot.models.assets import save_asset

    data = orjson.dumps(payload, option=orjson.OPT_UTC_Z | orjson.OPT_NAIVE_UTC, default=str)
    _write_featured_file(data)
    try:
        save_asset(FEATURED_KEY, "manifest", "application/json", data)
    except Exception as exc:
        log.warning("could not store the featured manifest in the database: %s", exc)


def _frame_requests(payload: dict[str, Any] | None) -> list[pcdata.RenderRequest]:
    """The GET /render requests behind a manifest's frame URLs (both kinds of every frame)."""
    if not payload or not payload.get("aoi") or not payload.get("frames"):
        return []
    b = _round_bbox(payload["aoi"]["bbox"])
    return [
        pcdata.RenderRequest(f["collection"], f["item_id"], kind, FEATURED_W, FEATURED_H,
                             pcdata.default_format(kind), bounds=b)
        for f in payload["frames"] for kind in ("truecolor", "swir")
    ]


CHIP_SCARS = 24  # case files stored ahead: the largest candidate unseen burns, as the job's prewarm


def case_chip_requests(session: Session, limit: int = CHIP_SCARS) -> list[pcdata.RenderRequest]:
    """The chips (pre and post pass, every kind) of the largest candidate unseen burns' case files,
    exactly as GET /api/scars/{id} links them, so the cache keys are the ones its pages ask for."""
    from blindspot.api.routers import (
        scars,  # the scars module owns the chip geometry and size
    )

    if not _table_exists(session, "burn_cluster"):
        return []
    rows = session.execute(text("""
        SELECT ST_AsGeoJSON(geom)::json, ST_Y(centroid), scene_pre, scene_post, collection
        FROM burn_cluster WHERE unseen IS TRUE ORDER BY area_ha DESC, id LIMIT :n
    """), {"n": limit}).all()
    out = []
    for geom, lat, pre, post, collection in rows:
        b = _round_bbox(scars._chip_bbox(geom, lat))
        for item in (pre, post):
            if item and pcdata.collection_for(item) == collection:
                out += [pcdata.RenderRequest(collection, item, kind, scars.CHIP_PX, scars.CHIP_PX,
                                             pcdata.default_format(kind), bounds=b) for kind in pcdata.KINDS]
    return out


def store_featured_assets(chip_scars: int = CHIP_SCARS) -> dict[str, Any]:
    """Keep the featured frames, the largest scars' case-file chips and the manifest in
    rendered_asset (rendering whatever is missing), then delete stored assets that nothing
    references and nobody used for a week. Run by the `featured` job, through refresh_featured()."""
    from blindspot.models.assets import KEEP_DAYS, prune_assets

    manifest = load_featured()
    wanted: dict[str, tuple[pcdata.RenderRequest, float, str]] = {}
    for req in _frame_requests(manifest):
        wanted[req.cache_key()] = (req, FRAME_MIN_VALID, "frame")
    with session_scope() as session:
        chips = case_chip_requests(session, chip_scars)
    for req in chips:
        wanted.setdefault(req.cache_key(), (req, CROP_MIN_VALID, "chip"))
    before = pcdata.stored_keys(list(wanted))

    def one(entry: tuple[pcdata.RenderRequest, float, str]) -> None:
        req, min_valid, role = entry
        try:
            pcdata.render(req, min_valid_fraction=min_valid, persist=role)
        except Exception as exc:  # recorded in source_fetch already; one bad chip must not stop the rest
            log.warning("featured asset %s %s failed: %s", req.item, req.kind, exc)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, [entry for key, entry in wanted.items() if key not in before]))
    if manifest is not None and not pcdata.stored_keys([FEATURED_KEY]):
        _store_featured(manifest)
    after = pcdata.stored_keys([*wanted, FEATURED_KEY])
    summary: dict[str, Any] = {
        "frames": sum(1 for entry in wanted.values() if entry[2] == "frame"),
        "chips": sum(1 for entry in wanted.values() if entry[2] == "chip"),
        "already_stored": len(before),
        "stored_now": len(after - before - {FEATURED_KEY}),
        "missing": len(set(wanted) - after),
        "manifest_stored": FEATURED_KEY in after,
    }
    if after:  # an empty answer may mean the database failed: then nothing is pruned
        summary["pruned"] = prune_assets(after, keep_days=KEEP_DAYS)
    return summary


_build_lock = threading.Lock()  # one build at a time per process


def _age(payload: dict[str, Any]) -> timedelta:
    try:
        built = datetime.fromisoformat(str(payload["built_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return timedelta.max
    return utcnow() - built


def refresh_featured(*, force: bool = False, store_assets: bool = True) -> dict[str, Any]:
    """Rebuild the featured frames when they are older than 6 h, the window has moved, or `force`;
    then (`store_assets`) keep the frames and the case-file chips in the database.

    Used by the `featured` job. Cheap when nothing changed: choosing the window is one query, and
    assets already stored cost one lookup. A build that finds no frames never replaces frames that
    worked."""
    summary = _rebuild_featured(force)
    if store_assets:
        try:
            summary["assets"] = store_featured_assets()
        except Exception as exc:  # the frames themselves are fine; say what failed
            log.exception("storing the featured assets failed")
            summary["assets"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return summary


def _rebuild_featured(force: bool) -> dict[str, Any]:
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
        refresh_featured(store_assets=False)  # inside the API: the frames only, never the chips
    except Exception:
        log.exception("background refresh of the featured frames failed")


@router.get("/featured")
def featured() -> ORJSONResponse:
    """The landing page's crossfade: frames of one 6.4 x 4 km window in Indian Punjab, chosen
    automatically from the data (most burned area in burn scars, or most FIRMS detections while
    there are no scars yet), one per clear Sentinel-2 pass of the last 20 days, oldest first.

    Served from a cache rebuilt by the `featured` job. When the cache is older than `stale_after_hours`
    the frames are still served, marked `stale` (with `age_minutes`). A long-lived process then
    rebuilds them in a background thread. A serverless instance (VERCEL set) never starts one:
    Vercel freezes the function after the response, so the work would be lost; the daily job on
    GitHub Actions refreshes the stored manifest instead.
    """
    serverless = on_vercel()
    payload = load_featured()
    if payload is None and serverless:
        # No stored manifest (or the database did not answer): building one here would take 20-60 s
        # of upstream calls in every cold instance, and would store no pictures. The job builds it.
        raise HTTPException(503, "the featured frames are not stored yet (the daily `featured` job builds them); "
                                 "try again later", headers={"Retry-After": "300"})
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
    age = _age(payload)
    stale_after = FEATURED_STALE_AFTER_SERVERLESS if serverless else FEATURED_TTL
    stale = age > stale_after
    if stale and not serverless and not _build_lock.locked():
        threading.Thread(target=_refresh_quietly, name="featured-refresh", daemon=True).start()
    sources = [SourceRef(**s) for s in payload.get("sources", [])]
    body = {k: v for k, v in payload.items() if k != "sources"}
    body["frames"] = [_rebased(frame) for frame in body.get("frames") or []]
    body["stale"] = stale
    body["age_minutes"] = None if age == timedelta.max else max(0, int(age.total_seconds() // 60))
    body["stale_after_hours"] = round(stale_after.total_seconds() / 3600, 2)
    return respond(body, sources, max_age=300)


def _rebased(frame: dict[str, Any]) -> dict[str, Any]:
    """The frame with its picture URLs on this deployment's PUBLIC_BASE_URL. The stored manifest
    keeps the base of whichever host built it (a job runner, a laptop: http://localhost:8000)."""
    base = get_settings().public_base_url
    out = dict(frame)
    for key in ("truecolor_url", "swir_url"):
        url = out.get(key)
        if isinstance(url, str) and "/api/imagery/render?" in url:
            out[key] = base + url[url.index("/api/imagery/render?"):]
    return out


__all__ = ["build_featured", "case_chip_requests", "densest_window", "refresh_featured", "render_url",
           "router", "select_window", "store_featured_assets"]
