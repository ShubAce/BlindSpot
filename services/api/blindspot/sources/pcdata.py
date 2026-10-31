"""Sentinel-2 pictures: renders from the Planetary Computer data API, with our own renderer as backup.

Who uses this: GET /api/imagery/render (before/after chips of a burn scar, the landing page frames)
and GET /api/imagery/tiles/... (a scene as a map layer).

Why two renderers. Planetary Computer (PC) runs a tiler that crops, colours and encodes a scene on
its own servers, which is fast and costs us almost nothing. But on 2026-10-30 PC served noise for
every 2026-10-28 Punjab item (docs/research/sentinel2-burnscars.md section 0, SYNTHESIS section 1
#2b): its tiler drew black squares. So:

1. Every PC item first passes a plausibility gate: the median B12 (short-wave infrared) reflectance
   over the whole item must lie in (0.02, 0.6), the research doc's scene gate. A corrupt item's
   median is negative; a real one is about 0.15.
2. If the gate fails, the PC tiler is down, or the item only exists on Earth Search (Earth Search
   gets new passes about 4 h after sensing, PC about 12 h), we read the same pixels from Earth
   Search's public cloud-optimised GeoTIFFs (COGs) and apply the same colour recipe ourselves.
3. Every finished picture is checked (not blank, not black) before anyone sees it, and cached on
   disk forever. We never return a placeholder: if nothing works, the caller gets an error.

The two catalogues name things differently; PC ids embed the processing time
(S2B_MSIL2A_20261028T053929_R005_T43REQ_20261028T092239) and Earth Search ids the tile sensing time
(S2B_T43REQ_20261028T054808_L2A). Earth Search's `s2:product_uri` holds the PC id, which is how a
scene's "twin" in the other catalogue is found.

Every upstream call is logged in source_fetch through http.fetch, or provenance.track_fetch for the
rasterio reads. The only table this module touches is rendered_asset (blindspot.models.assets):
render() reads the disk cache, then that table, then renders, and stores a picture there only when
the caller asks (`persist=`): the featured frames and the case-file chips, never map tiles.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
import orjson

from blindspot.config import get_settings
from blindspot.http import FetchError, SourceUnavailable, fetch, set_fetch_records
from blindspot.provenance import SourceInfo, register_source, track_fetch, utcnow

log = logging.getLogger(__name__)

PC_DATA = "https://planetarycomputer.microsoft.com/api/data/v1"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
E84_STAC = "https://earth-search.aws.element84.com/v1"
PC_S2 = "sentinel-2-l2a"  # Planetary Computer's Sentinel-2 L2A collection
E84_S2 = "sentinel-2-c1-l2a"  # Earth Search "Collection 1" Sentinel-2 L2A (the analytics catalogue)
COLLECTIONS: tuple[str, ...] = (PC_S2, E84_S2)

# Pixel reads from Earth Search COGs are a separate upstream from its STAC API, so they get their
# own source key (shown in GET /api/sources and cited on every picture we render ourselves).
register_source(
    SourceInfo(
        "s2_cog_e84",
        "Sentinel-2 L2A COGs on AWS (Earth Search)",
        "imagery",
        None,
        "https://registry.opendata.aws/sentinel-2-l2a-cogs/",
    )
)

# Item ids. Validated before use, so a request can never smuggle a path or a query into a URL.
# \Z, not $: `$` also matches before a trailing newline, which then reached a URL and a header.
PC_ID = re.compile(r"^(S2[A-D])_MSIL2A_(\d{8})T(\d{6})_R(\d{3})_T(\d{2}[A-Z]{3})_(\d{8})T(\d{6})\Z")
E84_ID = re.compile(r"^(S2[A-D])_T(\d{2}[A-Z]{3})_(\d{8})T(\d{6})_L2A\Z")

# Earth Search asset names for the Sentinel-2 bands we draw.
E84_ASSET = {"B02": "blue", "B03": "green", "B04": "red", "B8A": "nir08", "B11": "swir16",
             "B12": "swir22", "SCL": "scl"}

# Processing baseline 04.00+ (every 2026 scene is 05.13) stores reflectance as DN = 10000 * R + 1000,
# so R = (DN - 1000) / 10000 and DN 0 means "no data" (sentinel2-burnscars.md section 1, Gotchas).
DN_OFFSET = 1000.0
DN_SCALE = 10000.0
# Scene gate: median B12 reflectance of a real scene lies in (0.02, 0.6) (sentinel2-burnscars.md
# section 4 step 3 and SYNTHESIS section 5 #6). PC's corrupt 2026-10-28 items give -0.087.
B12_PLAUSIBLE = (0.02, 0.6)
# Scene Classification Layer classes (ESA Sentinel-2 L2A product spec): 0 no data, 1 saturated or
# defective, 3 cloud shadow, 8 cloud medium probability, 9 cloud high probability, 10 thin cirrus.
SCL_NODATA = (0, 1)
SCL_CLOUD = (3, 8, 9, 10)

RECIPE_VERSION = "r1"  # part of every cache key: change it when a colour recipe changes
# A 1600x1000 crop takes 1.5-4 s on PC and statistics ~1 s, so 20 s means something is wrong. fetch()
# makes up to 3 attempts, so a request waits at most ~65 s before it gets a JSON error.
UPSTREAM_TIMEOUT_S = httpx.Timeout(20.0, connect=5.0)
MAX_PARALLEL_RENDERS = 4  # renders in flight to upstream at once, per process


class RenderError(RuntimeError):
    """No renderer could produce a valid picture. The message lists every attempt and why it failed."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class RenderBusy(RenderError):
    """Too many pictures are being drawn at once; the caller should try again in a few seconds."""


# --- ids, catalogues and twins ---------------------------------------------------------------------


@dataclass(frozen=True)
class SceneId:
    """What identifies one Sentinel-2 acquisition of one MGRS tile, in either catalogue."""

    platform: str  # S2A / S2B / S2C
    tile: str  # MGRS tile, e.g. 43REQ
    day: date  # UTC date of the pass


def collection_for(item_id: str) -> str | None:
    """Which collection an item id belongs to, from its shape; None if it is neither."""
    if PC_ID.match(item_id):
        return PC_S2
    if E84_ID.match(item_id):
        return E84_S2
    return None


def scene_id(item_id: str) -> SceneId | None:
    m = PC_ID.match(item_id)
    if m:
        return SceneId(m.group(1), m.group(5), datetime.strptime(m.group(2), "%Y%m%d").date())
    m = E84_ID.match(item_id)
    if m:
        return SceneId(m.group(1), m.group(2), datetime.strptime(m.group(3), "%Y%m%d").date())
    return None


def pc_id_from_product_uri(product_uri: str | None) -> str | None:
    """Earth Search's s2:product_uri (S2B_MSIL2A_20261028T053929_N0513_R005_T43REQ_..._SAFE) minus the
    baseline token and the .SAFE suffix is exactly the PC item id."""
    if not product_uri:
        return None
    text = re.sub(r"_N\d{4}_", "_", product_uri.removesuffix(".SAFE"))
    return text if PC_ID.match(text) else None


# --- the MGRS tiles of the area we watch -------------------------------------------------------------
# A Sentinel-2 item covers one MGRS tile. The picture routes serve only items on the tiles that
# intersect the area we watch, checked from the item id alone, so a request for any other scene on
# Earth is refused before any upstream call. The tile list is derived from the AOI here (not typed
# in): it follows AOI_BBOX if that ever changes.
#
# Sentinel-2's tiling: each tile is a 100 km MGRS grid square of one UTM zone, extended to 109.8 km
# east and south of the square's north-west corner. Its name is zone + latitude band + the square's
# two letters. A square that crosses a latitude band boundary (8 degree bands; 32N runs through our
# AOI) appears under the band its top lies in (43SER covers 31.5-32.5N), so every band the square
# touches is accepted. Pure Python (Snyder's UTM series), because pyproj is not in the serverless bundle.

_WGS84_A = 6378137.0
_WGS84_F = 1 / 298.257223563
_UTM_K0 = 0.9996
_MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX"  # 8 degree latitude bands from 80S (X spans 72-84N)
_MGRS_COLUMNS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")  # column letter sets, zone 1, 2, 3, then repeat
_MGRS_ROWS = "ABCDEFGHJKLMNPQRSTUV"  # 20 row letters, repeating every 2,000 km of northing
S2_TILE_M = 109_800.0  # a Sentinel-2 tile's side
MGRS_SQUARE_M = 100_000.0


def utm_forward(lon: float, lat: float, zone: int) -> tuple[float, float]:
    """WGS84 lon/lat to UTM easting/northing (metres) in `zone` (Snyder 1987, eq. 8-9 to 8-10; better
    than a metre within a few degrees of the zone, which is all the tile test needs)."""
    e2 = _WGS84_F * (2 - _WGS84_F)
    ep2 = e2 / (1 - e2)
    phi = math.radians(lat)
    lam = math.radians(lon - ((zone - 1) * 6 - 180 + 3))
    sin, cos, tan = math.sin(phi), math.cos(phi), math.tan(phi)
    n = _WGS84_A / math.sqrt(1 - e2 * sin * sin)
    t, c, a = tan * tan, ep2 * cos * cos, cos * lam
    m = _WGS84_A * ((1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * phi
                    - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * phi)
                    + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * phi)
                    - (35 * e2**3 / 3072) * math.sin(6 * phi))
    x = _UTM_K0 * n * (a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * a**5 / 120)
    y = _UTM_K0 * (m + n * tan * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a**4 / 24
                                  + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * a**6 / 720))
    return 500_000.0 + x, y if lat >= 0 else y + 10_000_000.0


def _projected_box(zone: int, bbox: tuple[float, float, float, float], steps: int = 64) -> Any:
    """A lon/lat box as a polygon in UTM `zone`, its edges densified (they curve in UTM)."""
    from shapely.geometry import Polygon

    w, s, e, n = bbox
    edge = [i / steps for i in range(steps)]
    ring = ([(w + (e - w) * f, s) for f in edge] + [(e, s + (n - s) * f) for f in edge]
            + [(e - (e - w) * f, n) for f in edge] + [(w, n - (n - s) * f) for f in edge])
    return Polygon([utm_forward(lon, lat, zone) for lon, lat in ring])


def mgrs_tiles(bbox: tuple[float, float, float, float]) -> frozenset[str]:
    """Every Sentinel-2 tile name (e.g. 43REQ) whose 109.8 km footprint intersects `bbox`.

    A square exists in a zone (and a band) when it overlaps that zone's 6 degree strip (within that
    band); zones on either side of the box are tried, since a square at a zone's edge reaches past it.
    """
    from shapely.geometry import box as shapely_box

    w, s, e, n = bbox
    out: set[str] = set()
    zones = range(max(1, int((w + 180) // 6)), min(60, int((e + 180) // 6) + 2) + 1)
    bands = [(i, -80 + 8 * i) for i in range(len(_MGRS_BANDS)) if -80 + 8 * i < n + 1 and -80 + 8 * i + 8 > s - 1]
    for zone in zones:
        area = _projected_box(zone, bbox)
        strip_w = (zone - 1) * 6 - 180
        cells = {i: _projected_box(zone, (strip_w, lat0, strip_w + 6, lat0 + 8)) for i, lat0 in bands}
        minx, miny, maxx, maxy = area.bounds
        for col in range(int(minx // MGRS_SQUARE_M) - 1, int(maxx // MGRS_SQUARE_M) + 1):
            for row in range(int(miny // MGRS_SQUARE_M), int(maxy // MGRS_SQUARE_M) + 2):
                x0, y0 = col * MGRS_SQUARE_M, row * MGRS_SQUARE_M
                if not 1 <= col <= 8:
                    continue
                footprint = shapely_box(x0, y0 + MGRS_SQUARE_M - S2_TILE_M, x0 + S2_TILE_M, y0 + MGRS_SQUARE_M)
                if not footprint.intersects(area):
                    continue
                square = shapely_box(x0, y0, x0 + MGRS_SQUARE_M, y0 + MGRS_SQUARE_M)
                letters = (_MGRS_COLUMNS[(zone - 1) % 3][col - 1]
                           + _MGRS_ROWS[(row + (5 if zone % 2 == 0 else 0)) % 20])
                for i, cell in cells.items():
                    if square.intersection(cell).area > 0:
                        out.add(f"{zone:02d}{_MGRS_BANDS[i]}{letters}")
    return frozenset(out)


_watched: dict[tuple[float, float, float, float], frozenset[str]] = {}


def watched_tiles(bbox: tuple[float, float, float, float] | None = None, margin: float = 0.25) -> frozenset[str]:
    """The tiles of the area we watch (AOI_BBOX plus `margin` degrees, the picture routes' margin)."""
    if bbox is None:
        bbox = get_settings().aoi_bbox
    grown = (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)
    if grown not in _watched:
        _watched[grown] = mgrs_tiles(grown)
    return _watched[grown]


def item_tile(item_id: str) -> str | None:
    sid = scene_id(item_id)
    return sid.tile if sid else None


_item_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_item_lock = threading.Lock()
ITEM_TTL_S = 24 * 3600
# An item id that upstream does not know is remembered for an hour, so asking for it again (a typo,
# or someone cycling made-up ids) costs no further STAC call on this instance.
MISSING_TTL_S = 3600
_missing: dict[tuple[str, str], tuple[float, FetchError]] = {}


def get_item(collection: str, item_id: str) -> dict[str, Any]:
    """One STAC item (geometry, assets, properties), cached in memory for a day.

    Raises FetchError (404 when the item does not exist) or SourceUnavailable.
    """
    key = (collection, item_id)
    with _item_lock:
        hit = _item_cache.get(key)
        if hit and time.monotonic() - hit[0] < ITEM_TTL_S:
            return hit[1]
        gone = _missing.get(key)
        if gone and time.monotonic() - gone[0] < MISSING_TTL_S:
            raise gone[1]
    try:
        if collection == PC_S2:
            result = fetch("stac_pc", f"{PC_STAC}/collections/{PC_S2}/items/{item_id}")
        elif collection == E84_S2:
            result = fetch("stac_e84", f"{E84_STAC}/collections/{E84_S2}/items/{item_id}")
        else:
            raise ValueError(f"unknown collection {collection!r}")
    except FetchError as exc:
        if exc.status_code == 404:
            with _item_lock:
                if len(_missing) > 5000:
                    _missing.clear()
                _missing[key] = (time.monotonic(), exc)
        raise
    set_fetch_records(result.fetch_id, 1)
    with _item_lock:
        _item_cache[key] = (time.monotonic(), result.data)
    return result.data


@dataclass
class SearchResult:
    items: list[dict[str, Any]]
    fetch_ids: list[int] = field(default_factory=list)
    source_key: str = ""


def search(
    collection: str,
    intersects: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    query: dict[str, Any] | None = None,
    max_items: int = 200,
) -> SearchResult:
    """STAC search in one catalogue (PC or Earth Search), oldest first, following `next` pages.

    POST /search is used because a polygon does not fit comfortably in a URL. Each page is one
    logged fetch.
    """
    if collection == PC_S2:
        source_key, url = "stac_pc", f"{PC_STAC}/search"
    elif collection == E84_S2:
        source_key, url = "stac_e84", f"{E84_STAC}/search"
    else:
        raise ValueError(f"unknown collection {collection!r}")
    body: dict[str, Any] = {
        "collections": [collection],
        "intersects": intersects,
        "datetime": f"{_iso(start)}/{_iso(end)}",
        "limit": 100,
    }
    if query:
        body["query"] = query
    out = SearchResult(items=[], source_key=source_key)
    page_url: str | None = url
    while page_url and len(out.items) < max_items:
        result = fetch(source_key, page_url, method="POST", json=body, meta={"collection": collection})
        features = (result.data or {}).get("features", [])
        set_fetch_records(result.fetch_id, len(features))
        if result.fetch_id is not None:
            out.fetch_ids.append(result.fetch_id)
        out.items.extend(features)
        if len(features) < body["limit"]:
            break  # a short page is the last one (Earth Search still sends a `next` link)
        nxt = next((ln for ln in (result.data or {}).get("links", []) if ln.get("rel") == "next"), None)
        if not nxt:
            break
        page_url = nxt.get("href")
        body = nxt.get("body") or body  # STAC POST paging puts the next page's body in the link
    out.items.sort(key=lambda it: it["properties"].get("datetime", ""))
    return out


def find_twin(item_id: str) -> tuple[str, dict[str, Any]] | None:
    """The same acquisition in the other catalogue, as (collection, item), or None.

    Earth Search -> PC: the PC id is in s2:product_uri, so one GET does it. PC -> Earth Search:
    search that MGRS tile on that day and match the product URI back to the PC id.
    """
    sid = scene_id(item_id)
    if sid is None:
        return None
    if collection_for(item_id) == E84_S2:
        pc_id = pc_id_from_product_uri(get_item(E84_S2, item_id)["properties"].get("s2:product_uri"))
        if pc_id is None:
            return None
        try:
            return PC_S2, get_item(PC_S2, pc_id)
        except FetchError as exc:
            if exc.status_code == 404:  # PC has not ingested this pass yet
                return None
            raise
    start = datetime.combine(sid.day, datetime.min.time(), UTC)
    found = search(
        E84_S2,
        _tile_point(item_id),
        start,
        start + timedelta(days=1),
        query={"grid:code": {"eq": f"MGRS-{sid.tile}"}},
        max_items=20,
    )
    for item in found.items:
        if pc_id_from_product_uri(item["properties"].get("s2:product_uri")) == item_id:
            return E84_S2, item
    return None


def _tile_point(item_id: str) -> dict[str, Any]:
    """A point inside the PC item's footprint, to keep the twin search tiny."""
    geom = get_item(PC_S2, item_id)["geometry"]
    from shapely.geometry import shape

    p = shape(geom).representative_point()
    return {"type": "Point", "coordinates": [p.x, p.y]}


# --- the PC plausibility gate ----------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    ok: bool
    b12_median: float | None  # reflectance
    reason: str
    fetch_id: int | None = None


_gate_cache: dict[str, tuple[float, Gate]] = {}
_gate_lock = threading.Lock()
GATE_PASS_TTL_S = 7 * 24 * 3600
GATE_FAIL_TTL_S = 6 * 3600  # look again later: PC may repair an item


def pc_gate(item_id: str) -> Gate:
    """Are PC's pixels for this item real? One statistics call over the whole item (an overview
    read on PC's side, about 1 s), remembered for days (failures for 6 h)."""
    with _gate_lock:
        hit = _gate_cache.get(item_id)
    if hit and time.monotonic() - hit[0] < (GATE_PASS_TTL_S if hit[1].ok else GATE_FAIL_TTL_S):
        return hit[1]
    result = fetch(
        "pc_data",
        f"{PC_DATA}/item/statistics",
        params=[("collection", PC_S2), ("item", item_id), ("assets", "B12"), ("nodata", "0"),
                ("max_size", "256")],
        timeout=UPSTREAM_TIMEOUT_S,
        meta={"purpose": "plausibility gate", "item": item_id},
    )
    stats = (result.data or {}).get("B12_b1") or {}
    median_dn = stats.get("median")
    if median_dn is None:
        gate = Gate(False, None, "PC returned no B12 statistics", result.fetch_id)
    else:
        refl = (float(median_dn) - DN_OFFSET) / DN_SCALE
        ok = B12_PLAUSIBLE[0] < refl < B12_PLAUSIBLE[1]
        reason = "ok" if ok else (
            f"PC pixels fail the plausibility gate: median B12 reflectance {refl:.3f} is outside "
            f"{B12_PLAUSIBLE} (known PC data fault)"
        )
        gate = Gate(ok, round(refl, 4), reason, result.fetch_id)
    set_fetch_records(result.fetch_id, 1, {"b12_median": gate.b12_median, "gate_ok": gate.ok})
    with _gate_lock:
        _gate_cache[item_id] = (time.monotonic(), gate)
    return gate


# --- what we draw ------------------------------------------------------------------------------------

Kind = Literal["truecolor", "swir", "nbr"]
KINDS: tuple[str, ...] = ("truecolor", "swir", "nbr")

# PC's own "Natural color" formula for Sentinel-2 (sentinel2-burnscars.md section 5).
TRUECOLOR_FORMULA = "Gamma RGB 3.2 Saturation 0.8 Sigmoidal RGB 25 0.35"
# SWIR false colour (B12, B8A, B04): a linear stretch of DN 1000-5000, i.e. reflectance 0-0.4 once
# the +1000 offset is removed. Chosen over PC's SWIR colour formula after comparing both on real
# 2026-10-18/23 Punjab scenes: the formula washed fields out; this keeps burnt fields dark brown,
# harvested fields tan and green crops green.
SWIR_RESCALE = (DN_OFFSET, DN_OFFSET + 0.4 * DN_SCALE)
# NBR = (B8A - B12) / (B8A + B12) in reflectance; with the +1000 DN offset that is
# (B8A - B12) / (B8A + B12 - 2000) in DN. Shown from -0.5 (burnt) to 0.7 (green) on red-yellow-green.
NBR_EXPRESSION = "(B8A-B12)/(B8A+B12-2000)"
NBR_RESCALE = (-0.5, 0.7)


@dataclass(frozen=True)
class Recipe:
    bands: tuple[str, ...]  # Sentinel-2 band names, in output order
    pc_params: tuple[tuple[str, str], ...]  # query parameters for the PC tiler


RECIPES: dict[str, Recipe] = {
    "truecolor": Recipe(
        ("B04", "B03", "B02"),
        (("assets", "B04"), ("assets", "B03"), ("assets", "B02"), ("color_formula", TRUECOLOR_FORMULA)),
    ),
    "swir": Recipe(
        ("B12", "B8A", "B04"),
        (("assets", "B12"), ("assets", "B8A"), ("assets", "B04"),
         ("rescale", f"{SWIR_RESCALE[0]:g},{SWIR_RESCALE[1]:g}")),
    ),
    "nbr": Recipe(
        ("B8A", "B12"),
        (("expression", NBR_EXPRESSION), ("asset_as_band", "true"),
         ("rescale", f"{NBR_RESCALE[0]:g},{NBR_RESCALE[1]:g}"), ("colormap_name", "rdylgn")),
    ),
}
COMMON_PC_PARAMS = (("nodata", "0"), ("resampling", "bilinear"))


def media_type(fmt: str) -> str:
    return "image/jpeg" if fmt == "jpg" else "image/png"


def default_format(kind: str) -> str:
    """JPEG for the photographic composites (small), PNG for NBR (a colour map with transparency)."""
    return "png" if kind == "nbr" else "jpg"


# --- requests and results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderRequest:
    """One picture: an item, an area, a recipe and a size.

    A crop has `bounds` in lon/lat (EPSG:4326, like PC's bbox endpoint). An XYZ map tile has `tile`
    (z, x, y) and its Web Mercator bounds are derived from it.
    """

    collection: str
    item: str
    kind: str
    width: int
    height: int
    fmt: str
    bounds: tuple[float, float, float, float] | None = None
    tile: tuple[int, int, int] | None = None

    @property
    def crs(self) -> str:
        return "EPSG:3857" if self.tile else "EPSG:4326"

    def dst_bounds(self) -> tuple[float, float, float, float]:
        if self.tile:
            return mercator_tile_bounds(*self.tile)
        assert self.bounds is not None
        return self.bounds

    def cache_key(self) -> str:
        where = f"tile:{self.tile}" if self.tile else "bbox:" + ",".join(f"{v:.6f}" for v in self.bounds or ())
        raw = "|".join([RECIPE_VERSION, self.collection, self.item, self.kind, where,
                        f"{self.width}x{self.height}", self.fmt])
        return hashlib.sha256(raw.encode()).hexdigest()[:40]


@dataclass(frozen=True)
class Rendered:
    data: bytes
    media_type: str
    source_key: str  # pc_data (PC tiler) or s2_cog_e84 (our renderer)
    collection: str  # what was actually drawn (may be the twin of the requested item)
    item: str
    url: str | None  # the logged upstream URL (secrets masked; none here anyway)
    fetch_id: int | None
    rendered_at: datetime
    note: str | None = None  # why a fallback was used, if one was
    cached: bool = False

    def meta(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type, "source_key": self.source_key,
            "collection": self.collection, "item": self.item, "url": self.url,
            "fetch_id": self.fetch_id, "rendered_at": self.rendered_at.isoformat(), "note": self.note,
        }


EARTH_HALF = 20037508.342789244  # half the Web Mercator world width in metres (EPSG:3857)


def mercator_tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """XYZ tile -> (minx, miny, maxx, maxy) in EPSG:3857 metres (the standard slippy-map grid)."""
    size = 2 * EARTH_HALF / (2**z)
    minx = -EARTH_HALF + x * size
    maxy = EARTH_HALF - y * size
    return (minx, maxy - size, minx + size, maxy)


def tile_lonlat_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """XYZ tile -> lon/lat bounds (west, south, east, north)."""
    n = 2**z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return (x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y))


# --- checking a picture -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageCheck:
    ok: bool
    reason: str
    valid_fraction: float
    mean: float  # mean brightness (0-255) of the valid pixels
    std: float


# Thresholds measured on real 1600x1000 renders of tile 43REQ (2026-10-13..28): real scenes had a mean
# brightness of 85-193 and a spread (std) of 10-65; PC's corrupt 10-28 item rendered at mean 0.8.
MIN_MEAN = 12.0
MIN_STD = 2.0


def check_image(data: bytes, *, min_valid_fraction: float = 0.0) -> ImageCheck:
    """Is this a real picture? Not blank, not black, not one flat colour.

    Valid pixels are those with alpha > 0 (PNG) or any channel above 3 (JPEG has no alpha, so no-data
    comes out black). Map tiles accept any valid fraction (a scene edge is legitimate); crops ask
    for most of the area to be covered.
    """
    from PIL import Image  # pillow is imported on use: the API must start where it is not installed

    with Image.open(io.BytesIO(data)) as img:
        img.load()
        rgba = np.asarray(img.convert("RGBA"), dtype=np.float32)
    rgb, alpha = rgba[..., :3], rgba[..., 3]
    valid = (alpha > 0) & (rgb.max(axis=2) > 3)
    frac = float(valid.mean())
    if frac < 0.01:  # PC's corrupt items render like this: noise of DN 1-255 comes out black
        return ImageCheck(False, f"the picture is black or empty ({frac:.2%} of pixels have data)", frac, 0.0, 0.0)
    lum = rgb.mean(axis=2)[valid]
    mean, std = float(lum.mean()), float(lum.std())
    if frac < min_valid_fraction:
        return ImageCheck(False, f"only {frac:.0%} of the area has data", frac, mean, std)
    if mean < MIN_MEAN:
        return ImageCheck(False, f"the picture is black (mean brightness {mean:.1f}/255)", frac, mean, std)
    if std < MIN_STD:
        return ImageCheck(False, f"the picture is one flat colour (spread {std:.1f})", frac, mean, std)
    return ImageCheck(True, "ok", frac, mean, std)


# --- renderer 1: the PC tiler ----------------------------------------------------------------------


def render_pc(req: RenderRequest) -> Rendered:
    """Ask PC's tiler for the picture (bbox crop or XYZ tile). Raises FetchError / SourceUnavailable."""
    recipe = RECIPES[req.kind]
    params: list[tuple[str, str]] = [("collection", PC_S2), ("item", req.item)]
    params += list(recipe.pc_params) + list(COMMON_PC_PARAMS)
    if req.tile:
        z, x, y = req.tile
        url = f"{PC_DATA}/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.{req.fmt}"
    else:
        b = req.bounds
        assert b is not None
        url = f"{PC_DATA}/item/bbox/{b[0]:.6f},{b[1]:.6f},{b[2]:.6f},{b[3]:.6f}/{req.width}x{req.height}.{req.fmt}"
    result = fetch("pc_data", url, params=params, expect="bytes", timeout=UPSTREAM_TIMEOUT_S,
                   meta={"kind": req.kind, "item": req.item})
    set_fetch_records(result.fetch_id, 1)
    return Rendered(
        data=result.data, media_type=media_type(req.fmt), source_key="pc_data", collection=PC_S2,
        item=req.item, url=result.url_logged, fetch_id=result.fetch_id, rendered_at=utcnow(),
    )


# --- renderer 2: our own, from Earth Search COGs ---------------------------------------------------

_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",  # do not list the S3 "folder" on open
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_TIMEOUT": "20",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


def _read_band(href: str, dst_crs: str, dst_bounds: tuple[float, float, float, float],
               width: int, height: int, resampling_name: str) -> np.ndarray:
    """One band of a COG, warped onto the destination grid. Only the blocks under the area are read.

    The window is read at native resolution when the output is finer than the band (upsampling),
    and decimated otherwise, which makes GDAL use the COG's overviews instead of full-res blocks.
    Returns float32 DN with 0 where there is no data.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import reproject, transform_bounds
    from rasterio.windows import Window, from_bounds

    resampling = Resampling[resampling_name]
    with rasterio.Env(**_GDAL_ENV), rasterio.open(href) as src:
        left, bottom, right, top = transform_bounds(dst_crs, src.crs, *dst_bounds, densify_pts=21)
        win = from_bounds(left, bottom, right, top, transform=src.transform)
        col0 = max(0, math.floor(win.col_off) - 2)
        row0 = max(0, math.floor(win.row_off) - 2)
        col1 = min(src.width, math.ceil(win.col_off + win.width) + 2)
        row1 = min(src.height, math.ceil(win.row_off + win.height) + 2)
        dst = np.zeros((height, width), dtype=np.float32)
        if col1 <= col0 or row1 <= row0:
            return dst  # the area is outside this tile: all no-data
        win = Window(col0, row0, col1 - col0, row1 - row0)
        out_res = (right - left) / width  # destination pixel size in source units (metres)
        factor = max(1, int(out_res / abs(src.transform.a)))
        shape = (max(1, math.ceil(win.height / factor)), max(1, math.ceil(win.width / factor)))
        # Averaging blends values, which is right for reflectance and wrong for class codes (SCL).
        decimate = Resampling.nearest if resampling_name == "nearest" else Resampling.average
        data = src.read(1, window=win, out_shape=shape, resampling=decimate)
        wt = _scaled_transform(src.window_transform(win), win, shape)
        reproject(
            source=data.astype(np.float32), destination=dst,
            src_transform=wt, src_crs=src.crs, src_nodata=0,
            dst_transform=transform_from_bounds(*dst_bounds, width, height), dst_crs=dst_crs,
            dst_nodata=0, resampling=resampling,
        )
    return dst


def _scaled_transform(transform: Any, win: Any, shape: tuple[int, int]) -> Any:
    """The affine transform of a window read into `shape` (pixels get bigger when decimated)."""
    from affine import Affine

    return transform * Affine.scale(win.width / shape[1], win.height / shape[0])


def render_local(item: dict[str, Any], req: RenderRequest) -> Rendered:
    """Draw the picture ourselves from an Earth Search item's COGs, with the same recipe as PC."""
    recipe = RECIPES[req.kind]
    assets = item.get("assets", {})
    hrefs = []
    for band in recipe.bands:
        asset = assets.get(E84_ASSET[band])
        if not asset or not str(asset.get("href", "")).startswith("https://"):
            raise RenderError(f"{item.get('id')}: no public COG for band {band}", retryable=False)
        hrefs.append(asset["href"])
    bounds = req.dst_bounds()
    with track_fetch("s2_cog_e84", url=hrefs[0], meta={"item": item.get("id"), "kind": req.kind,
                                                      "bands": list(recipe.bands)}) as tracker:
        with ThreadPoolExecutor(len(hrefs)) as pool:
            bands = list(pool.map(
                lambda h: _read_band(h, req.crs, bounds, req.width, req.height, "bilinear"), hrefs
            ))
        tracker.records = len(bands)
    rgba = colourise(req.kind, bands)
    data = encode(rgba, req.fmt, palette=req.kind == "nbr")
    return Rendered(
        data=data, media_type=media_type(req.fmt), source_key="s2_cog_e84", collection=E84_S2,
        item=str(item.get("id")), url=hrefs[0], fetch_id=tracker.fetch_id, rendered_at=utcnow(),
    )


# --- the colour recipes, written out (they match PC's tiler: rio-tiler + rio-color) -----------------


def colourise(kind: str, bands: Sequence[np.ndarray]) -> np.ndarray:
    """DN bands -> an RGBA uint8 picture. Alpha is 0 wherever any band has no data (DN 0)."""
    mask = np.all([b > 0 for b in bands], axis=0)
    if kind == "truecolor":
        rgb = _colour_formula(np.stack(bands), TRUECOLOR_FORMULA)
    elif kind == "swir":
        rgb = _linear_stretch(np.stack(bands), *SWIR_RESCALE)
    elif kind == "nbr":
        nir, swir = bands
        with np.errstate(divide="ignore", invalid="ignore"):
            nbr = (nir - swir) / (nir + swir - 2 * DN_OFFSET)
        idx = _linear_stretch(np.nan_to_num(nbr, nan=NBR_RESCALE[0])[None], *NBR_RESCALE)[0]
        rgb = np.moveaxis(_rdylgn_lut()[idx], -1, 0)
    else:
        raise ValueError(f"unknown kind {kind!r}")
    rgba = np.zeros((rgb.shape[1], rgb.shape[2], 4), dtype=np.uint8)
    rgba[..., :3] = np.moveaxis(rgb, 0, -1)
    rgba[..., :3][~mask] = 0
    rgba[..., 3] = np.where(mask, 255, 0)
    return rgba


def encode(rgba: np.ndarray, fmt: str, *, palette: bool = False) -> bytes:
    """JPEG (quality 85) or PNG. `palette` writes an 8-bit palette PNG (~6x smaller) built from the
    picture's own colours, so it is exact; a picture with more than 256 colours stays RGBA."""
    from PIL import Image  # imported on use, see check_image()

    buf = io.BytesIO()
    if fmt == "jpg":
        Image.fromarray(rgba[..., :3], "RGB").save(buf, "JPEG", quality=85, optimize=True)
        return buf.getvalue()
    if palette:
        rgba = np.where(rgba[..., 3:4] == 0, 0, rgba).astype(np.uint8)  # one colour for "no data"
        packed = rgba.view(np.uint32).reshape(rgba.shape[:2])
        colours, index = np.unique(packed, return_inverse=True)
        if len(colours) <= 256:
            table = colours.astype(np.uint32).view(np.uint8).reshape(-1, 4)
            img = Image.fromarray(index.reshape(rgba.shape[:2]).astype(np.uint8), "P")
            img.putpalette(table[:, :3].ravel().tolist())
            img.save(buf, "PNG", optimize=True, transparency=bytes(table[:, 3].tolist()))
            return buf.getvalue()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _linear_stretch(arr: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """rio-tiler's `rescale`: map [lo, hi] linearly onto 0-255 and clip."""
    out = (arr.astype(np.float32) - lo) / (hi - lo) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def _colour_formula(dn: np.ndarray, formula: str) -> np.ndarray:
    """rio-color operations as PC's tiler applies them: each step scales the array to 0-1 by its
    dtype's maximum (65535 for the raw uint16 DN, then 255) and writes back uint8."""
    arr: np.ndarray = np.clip(dn, 0, 65535).astype(np.uint16)
    tokens = formula.split()
    i = 0
    while i < len(tokens):
        op = tokens[i].lower()
        x = arr.astype(np.float64) / np.iinfo(arr.dtype).max
        if op == "gamma":
            x = x ** (1.0 / float(tokens[i + 2]))
            i += 3
        elif op == "saturation":
            x = _saturate(x, float(tokens[i + 1]))
            i += 2
        elif op == "sigmoidal":
            x = _sigmoidal(x, float(tokens[i + 2]), float(tokens[i + 3]))
            i += 4
        else:
            raise ValueError(f"unsupported colour operation {op!r}")
        arr = (np.clip(x, 0, 1) * 255).astype(np.uint8)
    return arr


def _sigmoidal(x: np.ndarray, contrast: float, bias: float) -> np.ndarray:
    """rio-color sigmoidal contrast: an S-curve centred on `bias`, normalised to keep 0->0 and 1->1."""
    num = 1 / (1 + np.exp(contrast * (bias - x))) - 1 / (1 + np.exp(contrast * bias))
    den = 1 / (1 + np.exp(contrast * (bias - 1))) - 1 / (1 + np.exp(contrast * bias))
    return num / den


# sRGB <-> CIE XYZ (D65) matrices and white point, as in rio-color's colorspace module (IEC 61966-2-1).
_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                     [0.2126729, 0.7151522, 0.0721750],
                     [0.0193339, 0.1191920, 0.9503041]])
_XYZ2RGB = np.linalg.inv(_RGB2XYZ)
_WHITE = np.array([0.95047, 1.0, 1.08883])
_DELTA = 6.0 / 29  # CIE L*a*b* constants
_T0 = _DELTA**3


def _saturate(rgb: np.ndarray, mult: float) -> np.ndarray:
    """Multiply chroma in CIE LCh, which changes how vivid colours are without changing brightness
    (rio-color's `saturation`). `rgb` is (3, H, W) in 0-1."""
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = np.tensordot(_RGB2XYZ, lin, axes=1) / _WHITE[:, None, None]
    f = np.where(xyz > _T0, np.cbrt(xyz), xyz / (3 * _DELTA**2) + 4.0 / 29)
    L = 116 * f[1] - 16
    a = 500 * (f[0] - f[1]) * mult  # scaling a and b scales chroma and keeps the hue
    b = 200 * (f[1] - f[2]) * mult
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    ff = np.stack([fx, fy, fz])
    xyz = np.where(ff > _DELTA, ff**3, 3 * _DELTA**2 * (ff - 4.0 / 29)) * _WHITE[:, None, None]
    lin = np.clip(np.tensordot(_XYZ2RGB, xyz, axes=1), 0, None)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)


# ColorBrewer RdYlGn, 11 classes (colorbrewer2.org, Cynthia Brewer, Penn State): the anchor colours
# matplotlib and rio-tiler interpolate to build the "rdylgn" colour map.
_RDYLGN = ["a50026", "d73027", "f46d43", "fdae61", "fee08b", "ffffbf", "d9ef8b", "a6d96a", "66bd63",
           "1a9850", "006837"]


def _rdylgn_lut() -> np.ndarray:
    anchors = np.array([[int(h[i:i + 2], 16) for i in (0, 2, 4)] for h in _RDYLGN], dtype=np.float64)
    pos = np.linspace(0, 255, len(anchors))
    lut = np.stack([np.interp(np.arange(256), pos, anchors[:, c]) for c in range(3)], axis=1)
    return np.round(lut).astype(np.uint8)


# --- statistics inside an area (cloud cover, haze) ------------------------------------------------


@dataclass(frozen=True)
class AreaStats:
    """Scene classification and blue brightness inside an area, for choosing clear passes."""

    n_pixels: int
    nodata_fraction: float
    cloud_fraction: float  # cloud, cloud shadow and thin cirrus, among pixels with data
    blue_median: float | None  # B02 reflectance (rises with smoke haze)
    source_key: str
    fetch_ids: tuple[int, ...]

    @property
    def clear_fraction(self) -> float:
        return 1.0 - self.cloud_fraction


def _scl_fractions(classes: Sequence[float], counts: Sequence[float]) -> tuple[int, float, float]:
    total = float(sum(counts))
    if total <= 0:
        return 0, 1.0, 0.0
    by_class = {int(round(c)): float(n) for c, n in zip(classes, counts, strict=False)}
    nodata = sum(by_class.get(c, 0.0) for c in SCL_NODATA)
    cloud = sum(by_class.get(c, 0.0) for c in SCL_CLOUD)
    with_data = total - nodata
    return int(total), nodata / total, (cloud / with_data if with_data > 0 else 1.0)


def pc_area_stats(item_id: str, polygon: dict[str, Any]) -> AreaStats:
    """SCL class counts and the B02 median inside `polygon`, computed by PC's tiler (two small calls).

    SCL is intact even in PC's corrupt items (sentinel2-burnscars.md section 0), so the cloud part
    is usable for every PC item; the blue median only for items that pass the gate.
    """
    feature = {"type": "Feature", "properties": {}, "geometry": polygon}
    scl = fetch(
        "pc_data", f"{PC_DATA}/item/statistics", method="POST", json=feature,
        params=[("collection", PC_S2), ("item", item_id), ("assets", "SCL"), ("categorical", "true"),
                ("max_size", "1024")],
        timeout=UPSTREAM_TIMEOUT_S, meta={"purpose": "window cloud cover", "item": item_id},
    )
    stats = ((scl.data or {}).get("properties") or {}).get("statistics", {}).get("SCL_b1", {})
    counts, classes = (stats.get("histogram") or [[], []])[:2]
    n, nodata, cloud = _scl_fractions(classes, counts)
    set_fetch_records(scl.fetch_id, n)
    blue = fetch(
        "pc_data", f"{PC_DATA}/item/statistics", method="POST", json=feature,
        params=[("collection", PC_S2), ("item", item_id), ("assets", "B02"), ("nodata", "0"),
                ("max_size", "1024")],
        timeout=UPSTREAM_TIMEOUT_S, meta={"purpose": "window haze", "item": item_id},
    )
    bstats = ((blue.data or {}).get("properties") or {}).get("statistics", {}).get("B02_b1", {})
    med = bstats.get("median")
    set_fetch_records(blue.fetch_id, int(bstats.get("count") or 0))
    ids = tuple(i for i in (scl.fetch_id, blue.fetch_id) if i is not None)
    return AreaStats(n, nodata, cloud, None if med is None else (float(med) - DN_OFFSET) / DN_SCALE,
                     "pc_data", ids)


def e84_area_stats(item: dict[str, Any], bounds: tuple[float, float, float, float]) -> AreaStats:
    """The same numbers read from an Earth Search item's SCL and B02 COGs (lon/lat bounds)."""
    hrefs = {b: item["assets"][E84_ASSET[b]]["href"] for b in ("SCL", "B02")}
    # About 20 m pixels over the area: enough for fractions, cheap to read.
    w = max(8, round((bounds[2] - bounds[0]) * 111_320 * math.cos(math.radians((bounds[1] + bounds[3]) / 2)) / 20))
    h = max(8, round((bounds[3] - bounds[1]) * 111_320 / 20))
    with track_fetch("s2_cog_e84", url=hrefs["SCL"], meta={"item": item.get("id"), "purpose": "window stats"}) as t:
        scl = _read_band(hrefs["SCL"], "EPSG:4326", bounds, w, h, "nearest")
        b02 = _read_band(hrefs["B02"], "EPSG:4326", bounds, w, h, "nearest")
        t.records = int(scl.size)
    classes, counts = np.unique(np.round(scl).astype(int), return_counts=True)
    n, nodata, cloud = _scl_fractions(classes.tolist(), counts.tolist())
    vals = b02[b02 > 0]
    med = float(np.median(vals)) if vals.size else None
    ids = (t.fetch_id,) if t.fetch_id is not None else ()
    return AreaStats(n, nodata, cloud, None if med is None else (med - DN_OFFSET) / DN_SCALE,
                     "s2_cog_e84", ids)


# --- the disk cache --------------------------------------------------------------------------------


class ImageCache:
    """Rendered pictures on disk, keyed by their request, kept until the cache outgrows its budget.

    A Sentinel-2 scene never changes once published, so a picture of it is valid forever: that is
    what lets GET /api/imagery/render send `immutable` cache headers. Writes are atomic (temp file +
    rename) so a crash never leaves half a JPEG behind. When the folder grows past `max_bytes`, the
    least recently used files are deleted down to 80% of the budget.
    """

    PRUNE_EVERY = 200  # writes between size checks

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._writes = 0
        self._lock = threading.Lock()

    def _paths(self, key: str) -> tuple[Path, Path]:
        folder = self.root / key[:2]
        return folder / f"{key}.img", folder / f"{key}.json"

    def get(self, key: str) -> Rendered | None:
        img, meta = self._paths(key)
        try:
            data = img.read_bytes()
            info = orjson.loads(meta.read_bytes())
        except (OSError, ValueError):
            return None
        try:
            os.utime(img)  # mark as recently used, for pruning
        except OSError:
            pass
        return Rendered(
            data=data, media_type=info["media_type"], source_key=info["source_key"],
            collection=info["collection"], item=info["item"], url=info.get("url"),
            fetch_id=info.get("fetch_id"), rendered_at=datetime.fromisoformat(info["rendered_at"]),
            note=info.get("note"), cached=True,
        )

    def put(self, key: str, rendered: Rendered) -> None:
        img, meta = self._paths(key)
        img.parent.mkdir(parents=True, exist_ok=True)
        for path, payload in ((img, rendered.data), (meta, orjson.dumps(rendered.meta()))):
            tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        with self._lock:
            self._writes += 1
            due = self._writes % self.PRUNE_EVERY == 0
        if due:
            self.prune()

    def prune(self) -> int:
        """Delete least-recently-used pictures until the folder is under 80% of its budget."""
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.root.rglob("*.img")]
        total = sum(size for _, size, _ in files)
        removed = 0
        if total <= self.max_bytes:
            return 0
        for _, size, path in sorted(files):
            if total <= 0.8 * self.max_bytes:
                break
            for p in (path, path.with_suffix(".json")):
                try:
                    p.unlink()
                except OSError:
                    pass
            total -= size
            removed += 1
        log.info("imagery cache pruned: %d pictures removed", removed)
        return removed


CACHE_MAX_BYTES = 2 * 1024**3  # 2 GB of pictures; a 1600x1000 frame is ~300 KB, a map tile ~100 KB
_cache: ImageCache | None = None
_cache_guard = threading.Lock()


def image_cache() -> ImageCache:
    global _cache
    with _cache_guard:
        if _cache is None:
            _cache = ImageCache(get_settings().cache_dir / "imagery", CACHE_MAX_BYTES)
        return _cache


# --- render(): cache, single flight, gate, fallback --------------------------------------------------

_flights: dict[str, threading.Lock] = {}
_flights_guard = threading.Lock()
_upstream_slots = threading.BoundedSemaphore(MAX_PARALLEL_RENDERS)
# Routes run in a thread pool (~40 threads) shared by the whole API. A map opening a slow scene can
# ask for dozens of tiles at once; without this cap they would all sit waiting for a render slot and
# starve every other endpoint. Cache hits never count; past the cap a request gets a 503 at once.
MAX_ADMITTED_RENDERS = 12
SLOT_WAIT_S = 60.0
_admitted = threading.BoundedSemaphore(MAX_ADMITTED_RENDERS)


@contextmanager
def _single_flight(key: str) -> Iterator[None]:
    """Only one thread renders a given picture; others wait and then read it from the cache."""
    with _flights_guard:
        lock = _flights.setdefault(key, threading.Lock())
    with lock:
        yield
    with _flights_guard:
        if not lock.locked():
            _flights.pop(key, None)


def lookup(req: RenderRequest, *, stored: bool = True) -> Rendered | None:
    """The picture for `req` if it is already drawn: on this instance's disk, else (`stored`, crops
    only) in rendered_asset, copied to disk on the way. Never renders, never calls upstream."""
    cache = image_cache()
    key = req.cache_key()
    hit = cache.get(key)
    if hit is None and stored and req.tile is None:
        hit = load_stored(key)
        if hit is not None:
            cache.put(key, hit)
    return hit


def render(req: RenderRequest, *, min_valid_fraction: float = 0.0, persist: str | None = None,
           stored_lookup: bool = True) -> Rendered:
    """The picture for `req`: from the disk cache, else the database (rendered_asset), else
    rendered, checked and cached on disk.

    `persist` ("frame" or "chip") also keeps the picture in the database, so every serverless
    instance can serve it without rendering; map tiles are never looked up or stored there.
    `stored_lookup=False` skips the database lookup (the caller just looked, see lookup()).
    Raises RenderError when every renderer failed (the message lists each attempt), or
    SourceUnavailable when the only upstream that could serve it is paused.
    """
    cache = image_cache()
    key = req.cache_key()
    hit = cache.get(key)
    if hit is None and req.tile is None and stored_lookup:
        hit = load_stored(key)
        if hit is not None:
            cache.put(key, hit)
            return hit  # already in the database: nothing to persist
    if hit is not None:
        if persist and not stored_keys([key]):
            store(key, hit, persist)
        return hit
    if not _admitted.acquire(blocking=False):
        raise RenderBusy("too many pictures are being drawn right now; try again in a few seconds")
    try:
        with _single_flight(key):
            hit = cache.get(key)
            if hit is not None:
                return hit
            if not _upstream_slots.acquire(timeout=SLOT_WAIT_S):
                raise RenderBusy("every renderer was busy for a minute; try again shortly")
            try:
                rendered = _render_uncached(req, min_valid_fraction)
            finally:
                _upstream_slots.release()
            cache.put(key, rendered)
            if persist:
                store(key, rendered, persist)
            return rendered
    finally:
        _admitted.release()


# --- the database copy (rendered_asset) -------------------------------------------------------------
# Never fatal: when the database or the table is unavailable the picture is still served (from disk
# or a fresh render) and only the shared copy is missing.


def load_stored(key: str) -> Rendered | None:
    """The picture stored under `key` in rendered_asset, or None."""
    from blindspot.models.assets import load_asset

    try:
        row = load_asset(key)
    except Exception as exc:  # SQLAlchemyError: no database, no table yet
        log.warning("rendered_asset read of %s failed: %s", key, get_settings().redact(str(exc))[:200])
        return None
    if row is None or row["role"] == "manifest":
        return None
    return Rendered(
        data=row["data"], media_type=row["media_type"], source_key=row["source_key"],
        collection=row["collection"], item=row["item"], url=row["url"], fetch_id=row["fetch_id"],
        rendered_at=row["rendered_at"], note=row["note"], cached=True,
    )


def store(key: str, out: Rendered, role: str) -> bool:
    """Keep `out` in rendered_asset under `key` (role "frame" or "chip"). False if that failed."""
    from blindspot.models.assets import save_asset

    try:
        save_asset(key, role, out.media_type, out.data, source_key=out.source_key,
                   collection=out.collection, item=out.item, url=out.url, fetch_id=out.fetch_id,
                   rendered_at=out.rendered_at, note=out.note)
    except Exception as exc:
        log.warning("rendered_asset write of %s failed: %s", key, get_settings().redact(str(exc))[:200])
        return False
    return True


def stored_keys(keys: Sequence[str]) -> set[str]:
    """Which of `keys` are in rendered_asset (and mark them used). Empty when the table is not there."""
    from blindspot.models import assets

    try:
        return assets.stored_keys(keys)
    except Exception as exc:
        log.warning("rendered_asset lookup failed: %s", get_settings().redact(str(exc))[:200])
        return set()


def _render_uncached(req: RenderRequest, min_valid_fraction: float) -> Rendered:
    attempts: list[str] = []
    paused: list[SourceUnavailable] = []

    def accept(out: Rendered, label: str) -> Rendered | None:
        check = check_image(out.data, min_valid_fraction=min_valid_fraction)
        if check.ok:
            return out
        attempts.append(f"{label}: {check.reason}")
        return None

    def try_pc(item_id: str) -> Rendered | None:
        try:
            gate = pc_gate(item_id)
        except SourceUnavailable as exc:
            paused.append(exc)
            attempts.append(f"PC {item_id}: {exc}")
            return None
        except FetchError as exc:
            if exc.status_code == 404:  # PC has no such item: nothing to retry
                raise RenderError(f"Planetary Computer has no item {item_id}", retryable=False) from None
            attempts.append(f"PC {item_id}: {exc}")
            return None
        if not gate.ok:
            attempts.append(f"PC {item_id}: {gate.reason}")
            return None
        try:
            return accept(render_pc(_with_item(req, PC_S2, item_id)), f"PC {item_id}")
        except SourceUnavailable as exc:
            paused.append(exc)
            attempts.append(f"PC {item_id}: {exc}")
        except FetchError as exc:
            if exc.status_code == 404 and req.tile:
                raise RenderError("this map tile lies outside the scene", retryable=False) from None
            attempts.append(f"PC {item_id}: {exc}")
        return None

    def try_local(item: dict[str, Any]) -> Rendered | None:
        try:
            return accept(render_local(item, _with_item(req, E84_S2, item["id"])), f"Earth Search {item['id']}")
        except RenderError as exc:
            attempts.append(str(exc))
        except Exception as exc:  # a network or GDAL error while reading COGs
            attempts.append(f"Earth Search {item.get('id')}: {type(exc).__name__}: {exc}")
        return None

    def twin() -> tuple[str, dict[str, Any]] | None:
        try:
            return find_twin(req.item)
        except FetchError as exc:
            attempts.append(f"twin lookup for {req.item}: {exc}")
            return None

    if req.collection == PC_S2:
        out = try_pc(req.item)
        if out is None:
            found = twin()
            if found is not None and found[0] == E84_S2:
                out = try_local(found[1])
                if out is not None:
                    out = _noted(out, f"PC could not render {req.item} ({attempts[0]}); "
                                      f"drawn from the same pass on Earth Search")
    else:
        try:
            item = get_item(E84_S2, req.item)
        except FetchError as exc:
            if exc.status_code == 404:
                raise RenderError(f"no Earth Search item {req.item}", retryable=False) from None
            attempts.append(f"Earth Search item {req.item}: {exc}")
            item = None
        # PC holds byte-identical pixels for most passes (sentinel2-burnscars.md section 1) and its
        # tiler answers in ~1 s from India, where our own COG reads take 4-20 s. So use PC's copy
        # when it passes the gate, and draw it ourselves only when it does not (or PC lacks it).
        out = None
        if item is not None:
            found = twin()
            if found is not None and found[0] == PC_S2:
                try:
                    out = try_pc(found[1]["id"])
                except RenderError as exc:  # PC lost the twin: fall through to our own renderer
                    attempts.append(str(exc))
                if out is not None:
                    out = _noted(out, f"drawn by Planetary Computer from {found[1]['id']}, the same "
                                      f"pass as {req.item} (identical pixels, faster to serve)")
            if out is None:
                out = try_local(item)
    if out is not None:
        return out
    if paused and len(paused) == len(attempts):
        raise paused[0]
    raise RenderError("; ".join(attempts) or "no renderer could draw this picture")


def _with_item(req: RenderRequest, collection: str, item_id: str) -> RenderRequest:
    return RenderRequest(collection, item_id, req.kind, req.width, req.height, req.fmt, req.bounds, req.tile)


def _noted(out: Rendered, note: str) -> Rendered:
    return Rendered(out.data, out.media_type, out.source_key, out.collection, out.item, out.url,
                    out.fetch_id, out.rendered_at, note[:500], out.cached)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
