"""Sentinel-2 L2A scenes and the ESA WorldCover cropland mask, read from public STAC catalogues.

- Scenes come from Earth Search (Element 84) `sentinel-2-c1-l2a`: keyless, public Cloud-Optimised
  GeoTIFFs on AWS, about 4 h after sensing, with reflectance scale/offset in the metadata. Planetary
  Computer's copy of the 2026-10-28 passes is corrupt (docs/research/SYNTHESIS.md, item 2b), so the
  analysis reads Earth Search only and every pass still goes through a plausibility gate.
- The cropland mask is ESA WorldCover 2021 (class 40) from Planetary Computer, whose files need a
  short-lived signature (the planetary-computer package fetches it).

Nothing here writes to the database. Pixel reads are plain functions; the sentinel job wraps them
in provenance.track_fetch so every batch of reads leaves a source_fetch row.
"""

from __future__ import annotations

import datetime as dt
import logging
import warnings
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from blindspot.http import USER_AGENT
from blindspot.provenance import SourceInfo, register_source, track_fetch

log = logging.getLogger(__name__)
# rasterio logs "boto3 not available" at INFO for every https open; it is irrelevant here.
logging.getLogger("rasterio.session").setLevel(logging.WARNING)

E84_STAC = "https://earth-search.aws.element84.com/v1"
E84_COLLECTION = "sentinel-2-c1-l2a"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
WORLDCOVER_COLLECTION = "esa-worldcover"
WORLDCOVER_CROPLAND = 40  # WorldCover class "Cropland" (ESA WorldCover 2021 v200 product user manual)

# Our band names (Sentinel-2 MSI band numbers) -> Earth Search asset keys.
E84_ASSETS = {
    "B02": "blue", "B03": "green", "B04": "red", "B06": "rededge2", "B07": "rededge3",
    "B08": "nir", "B8A": "nir08", "B11": "swir16", "B12": "swir22", "SCL": "scl",
}
REFLECTANCE_BANDS = ("B02", "B03", "B04", "B06", "B07", "B08", "B8A", "B11", "B12")
MGRS_SQUARE_M = 100_000  # each MGRS tile owns one 100 km grid square; tiles overlap by ~9.8 km

# GDAL settings for reading COGs over HTTPS: no directory listing, merged range requests,
# HTTP/2 multiplexing, retries on 429/5xx, and a small block cache so memory stays bounded.
GDAL_OPTIONS: dict[str, Any] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_CONNECTTIMEOUT": "15",
    "GDAL_HTTP_USERAGENT": USER_AGENT,
    "GDAL_CACHEMAX": 64,  # MB; rasterio wants an int here
    "VSI_CACHE": "FALSE",
}

register_source(
    SourceInfo(
        "stac_e84",
        "Earth Search STAC: Sentinel-2 L2A Collection 1",
        "imagery",
        6 * 60,
        "https://element84.com/earth-search/",
    )
)
register_source(
    SourceInfo(
        "s2_cog",
        "Sentinel-2 L2A pixels (Earth Search COGs on AWS)",
        "imagery",
        6 * 60,
        "https://registry.opendata.aws/sentinel-2-l2a-cogs/",
    )
)
register_source(
    SourceInfo(
        "worldcover",
        "ESA WorldCover 2021 v200 cropland (Planetary Computer)",
        "landcover",
        None,
        "https://planetarycomputer.microsoft.com/dataset/esa-worldcover",
    )
)


@dataclass(frozen=True)
class SceneItem:
    """The parts of one STAC item the pipeline needs."""

    item_id: str
    collection: str
    datetime: dt.datetime  # tile sensing time, UTC
    mgrs_tile: str  # "43REQ"
    platform: str | None
    relative_orbit: int | None
    cloud_pct: float | None
    nodata_pct: float | None
    bbox: tuple[float, float, float, float]
    geometry: dict[str, Any]  # GeoJSON footprint of valid data
    epsg: int
    transform20: tuple[float, ...]  # the 20 m grid: (a, b, c, d, e, f) of an Affine
    shape20: tuple[int, int]
    hrefs: dict[str, str]  # band -> COG URL
    scale: dict[str, float] = field(default_factory=dict)  # band -> DN scale
    offset: dict[str, float] = field(default_factory=dict)  # band -> DN offset
    self_href: str | None = None

    @property
    def obs_date(self) -> dt.date:
        return self.datetime.astimezone(dt.UTC).date()


def _relative_orbit(props: dict[str, Any]) -> int | None:
    """Earth Search c1 has no sat:relative_orbit; the product URI carries it (..._R005_...)."""
    if props.get("sat:relative_orbit") is not None:
        return int(props["sat:relative_orbit"])
    for part in str(props.get("s2:product_uri", "")).split("_"):
        if len(part) == 4 and part.startswith("R") and part[1:].isdigit():
            return int(part[1:])
    return None


def parse_item(item: Any) -> SceneItem:
    """A pystac Item from Earth Search -> SceneItem. Raises KeyError/ValueError on a malformed item."""
    props = item.properties
    grid = str(props.get("grid:code", ""))  # "MGRS-43REQ"
    tile = grid.split("-", 1)[1] if grid.startswith("MGRS-") else str(props.get("s2:mgrs_tile", ""))
    if not tile:
        raise ValueError(f"{item.id}: no MGRS tile code")
    ref = item.assets[E84_ASSETS["B12"]]  # a native 20 m band defines the grid
    transform = ref.extra_fields.get("proj:transform")
    shape = ref.extra_fields.get("proj:shape")
    code = props.get("proj:code") or ref.extra_fields.get("proj:code")
    epsg = props.get("proj:epsg") or ref.extra_fields.get("proj:epsg")
    if epsg is None and isinstance(code, str) and code.upper().startswith("EPSG:"):
        epsg = int(code.split(":")[1])
    if transform is None or shape is None or epsg is None:
        raise ValueError(f"{item.id}: missing projection metadata")
    hrefs, scale, offset = {}, {}, {}
    for band, key in E84_ASSETS.items():
        asset = item.assets[key]
        hrefs[band] = asset.href
        bands_meta = asset.extra_fields.get("raster:bands") or [{}]
        scale[band] = float(bands_meta[0].get("scale", 1.0))
        offset[band] = float(bands_meta[0].get("offset", 0.0))
    self_link = next((link.href for link in item.links if link.rel == "self"), None)
    return SceneItem(
        item_id=item.id,
        collection=item.collection_id or E84_COLLECTION,
        datetime=item.datetime.astimezone(dt.UTC),
        mgrs_tile=tile,
        platform=props.get("platform"),
        relative_orbit=_relative_orbit(props),
        cloud_pct=props.get("eo:cloud_cover"),
        nodata_pct=props.get("s2:nodata_pixel_percentage"),
        bbox=tuple(float(v) for v in item.bbox),  # type: ignore[arg-type]
        geometry=item.geometry,
        epsg=int(epsg),
        transform20=tuple(float(v) for v in transform[:6]),
        shape20=(int(shape[0]), int(shape[1])),
        hrefs=hrefs,
        scale=scale,
        offset=offset,
        self_href=self_link,
    )


def search_scenes(
    intersects: dict[str, Any], start: dt.datetime, end: dt.datetime
) -> tuple[list[SceneItem], int | None]:
    """Every Earth Search S2 L2A item touching `intersects` (GeoJSON) sensed in [start, end].

    One source_fetch row covers the whole (paginated) search. Returns (items, fetch_id).
    """
    from pystac_client import Client

    span = f"{start.astimezone(dt.UTC):%Y-%m-%dT%H:%M:%SZ}/{end.astimezone(dt.UTC):%Y-%m-%dT%H:%M:%SZ}"
    url = f"{E84_STAC}/search?collections={E84_COLLECTION}&datetime={span}"
    with track_fetch("stac_e84", url=url, meta={"datetime": span}) as tracker:
        with warnings.catch_warnings():
            # pystac warns that it cannot parse the bucket of these https hrefs; harmless here.
            warnings.simplefilter("ignore", UserWarning)
            client = Client.open(E84_STAC, headers={"User-Agent": USER_AGENT})
            search = client.search(
                collections=[E84_COLLECTION], intersects=intersects, datetime=span, limit=200
            )
            raw = list(search.items())
        tracker.records = len(raw)
    scenes = []
    for item in raw:
        try:
            scenes.append(parse_item(item))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("skipping STAC item %s: %s", getattr(item, "id", "?"), exc)
    return scenes, tracker.fetch_id


# --- tile geometry -------------------------------------------------------------------------------


@dataclass(frozen=True)
class TileGrid:
    """A tile's pixel grid at one resolution, and the 100 km MGRS square it owns.

    Neighbouring S2 tiles overlap by ~9.8 km. A pixel is processed only by the tile whose MGRS
    square contains the pixel centre, so a burn in an overlap is counted once, never twice.
    """

    tile: str
    epsg: int
    res: int  # metres
    x0: float  # upper-left corner of the tile (same at every resolution)
    y0: float
    width: int  # pixels at `res`
    height: int
    core: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y) of the owned square

    @classmethod
    def from_scene(cls, scene: SceneItem, res: int) -> TileGrid:
        a, _b, c, _d, e, f = scene.transform20
        if abs(a - 20) > 1e-6 or abs(e + 20) > 1e-6:
            raise ValueError(f"{scene.item_id}: unexpected 20 m grid {scene.transform20}")
        height20, width20 = scene.shape20
        factor = res / 20
        core_x0 = round(c / MGRS_SQUARE_M) * MGRS_SQUARE_M
        core_y1 = round(f / MGRS_SQUARE_M) * MGRS_SQUARE_M
        return cls(
            tile=scene.mgrs_tile,
            epsg=scene.epsg,
            res=res,
            x0=c,
            y0=f,
            width=int(width20 / factor),
            height=int(height20 / factor),
            core=(core_x0, core_y1 - MGRS_SQUARE_M, core_x0 + MGRS_SQUARE_M, core_y1),
        )

    @property
    def transform(self) -> Affine:
        return Affine(self.res, 0.0, self.x0, 0.0, -self.res, self.y0)

    def window_bounds(self, window: Window) -> tuple[float, float, float, float]:
        x_min = self.x0 + window.col_off * self.res
        y_max = self.y0 - window.row_off * self.res
        return (x_min, y_max - window.height * self.res, x_min + window.width * self.res, y_max)

    def window_transform(self, window: Window) -> Affine:
        x_min, _, _, y_max = self.window_bounds(window)
        return Affine(self.res, 0.0, x_min, 0.0, -self.res, y_max)

    def blocks(self, size: int) -> list[Window]:
        """Square windows of `size` pixels, aligned with the COGs' internal tiles (so a window never
        drags in neighbouring compressed tiles), keeping only those that touch the owned square."""
        out = []
        for row in range(0, self.height, size):
            for col in range(0, self.width, size):
                window = Window(col, row, min(size, self.width - col), min(size, self.height - row))
                x_min, y_min, x_max, y_max = self.window_bounds(window)
                cx0, cy0, cx1, cy1 = self.core
                if x_max > cx0 and x_min < cx1 and y_max > cy0 and y_min < cy1:
                    out.append(window)
        return out

    def core_mask(self, window: Window) -> np.ndarray:
        """True where the pixel centre lies in the owned square ([x0, x1) x (y0, y1])."""
        x_min, _, _, y_max = self.window_bounds(window)
        xs = x_min + (np.arange(window.width) + 0.5) * self.res
        ys = y_max - (np.arange(window.height) + 0.5) * self.res
        cx0, cy0, cx1, cy1 = self.core
        in_x = (xs >= cx0) & (xs < cx1)
        in_y = (ys > cy0) & (ys <= cy1)
        return in_y[:, None] & in_x[None, :]


# --- pixel reads ----------------------------------------------------------------------------------


def _read_one(href: str, bounds: tuple[float, float, float, float], shape: tuple[int, int],
              resampling: Resampling) -> np.ndarray:
    """One COG window resampled onto `shape` (GDAL picks the matching overview when coarser)."""
    with rasterio.Env(**GDAL_OPTIONS), rasterio.open(href) as ds:
        window = rasterio.windows.from_bounds(*bounds, transform=ds.transform)
        window = Window(
            round(window.col_off), round(window.row_off), round(window.width), round(window.height)
        )
        # Block windows always lie inside the tile, so no boundless read (that path goes through a
        # warped VRT and would skip the COG overviews).
        return ds.read(1, window=window, out_shape=shape, resampling=resampling)


@dataclass
class BlockRead:
    """One pass (all items of a tile on one date) over one block: raw DN per band, mosaicked."""

    dn: dict[str, np.ndarray]  # band -> uint16 DN (SCL uint8); 0 = no data
    item_index: np.ndarray  # per pixel: position in `items` of the item that supplied it (255 = none)
    items: list[str]
    windows_read: int


def read_pass_block(
    scenes: Sequence[SceneItem],
    bounds: tuple[float, float, float, float],
    shape: tuple[int, int],
    bands: Iterable[str] = (*REFLECTANCE_BANDS, "SCL"),
    workers: int = 12,
) -> BlockRead:
    """Read `bands` for every item of one pass over `bounds`, then mosaic: the item with the least
    nodata fills first, later items fill only its holes. Same-date items are one datatake split into
    granules, or a neighbouring orbit minutes later, so their pixels never disagree in time."""
    scenes = sorted(scenes, key=lambda s: (s.nodata_pct if s.nodata_pct is not None else 100.0))
    bands = list(bands)
    jobs = [(i, band) for i in range(len(scenes)) for band in bands]

    def work(job: tuple[int, str]) -> tuple[int, str, np.ndarray]:
        i, band = job
        resampling = Resampling.nearest if band == "SCL" else Resampling.average
        return i, band, _read_one(scenes[i].hrefs[band], bounds, shape, resampling)

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        results = list(pool.map(work, jobs))
    per_item: dict[int, dict[str, np.ndarray]] = {}
    for i, band, array in results:
        per_item.setdefault(i, {})[band] = array
    mosaic: dict[str, np.ndarray] = {}
    item_index = np.full(shape, 255, dtype=np.uint8)
    for i in range(len(scenes)):
        arrays = per_item[i]
        has = arrays["SCL"] > 0 if "SCL" in arrays else arrays[bands[0]] > 0
        take = has & (item_index == 255)
        item_index[take] = i
        for band, array in arrays.items():
            if band not in mosaic:
                mosaic[band] = np.zeros(shape, dtype=array.dtype)
            mosaic[band][take] = array[take]
    return BlockRead(mosaic, item_index, [s.item_id for s in scenes], len(jobs))


def reflectance(dn: np.ndarray, scale: float, offset: float) -> np.ndarray:
    """DN -> surface reflectance (0-1) as float32, NaN where DN is 0 (no data).

    Earth Search c1 publishes scale 0.0001 and offset -0.1 for processing baseline 05.x, i.e.
    (DN - 1000) / 10000: the +1000 DN offset ESA added in baseline 04.00."""
    out = dn.astype(np.float32) * np.float32(scale) + np.float32(offset)
    out[dn == 0] = np.nan
    return out


# --- WorldCover ----------------------------------------------------------------------------------


def worldcover_hrefs(bbox_ll: tuple[float, float, float, float]) -> tuple[list[str], int | None]:
    """Signed URLs of the WorldCover 2021 3x3-degree COGs covering a lon/lat box."""
    import planetary_computer
    from pystac_client import Client

    url = f"{PC_STAC}/search?collections={WORLDCOVER_COLLECTION}&bbox={','.join(f'{v:.4f}' for v in bbox_ll)}"
    with track_fetch("worldcover", url=url) as tracker:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            client = Client.open(PC_STAC, headers={"User-Agent": USER_AGENT})
            items = list(client.search(collections=[WORLDCOVER_COLLECTION], bbox=list(bbox_ll)).items())
        # WorldCover has a 2020 (v100) and a 2021 (v200) edition; v200 has the better cropland class.
        items = [i for i in items if "2021" in i.id] or items
        hrefs = [planetary_computer.sign(i.assets["map"].href) for i in items]
        tracker.records = len(hrefs)
        tracker.meta["items"] = [i.id for i in items]
    return hrefs, tracker.fetch_id


def read_worldcover(hrefs: Sequence[str], epsg: int, transform: Affine, shape: tuple[int, int]) -> np.ndarray:
    """WorldCover classes reprojected onto a UTM block grid by majority vote (mode), uint8."""
    out = np.zeros(shape, dtype=np.uint8)
    with rasterio.Env(**GDAL_OPTIONS):
        for href in hrefs:
            with rasterio.open(href) as src, WarpedVRT(
                src, crs=f"EPSG:{epsg}", transform=transform, width=shape[1], height=shape[0],
                resampling=Resampling.mode,
            ) as vrt:
                array = vrt.read(1)
            fill = (out == 0) & (array != 0)
            out[fill] = array[fill]
    return out


def utm_bounds_to_lonlat(epsg: int, bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """A UTM box -> the lon/lat box that contains it (corners and edge midpoints)."""
    from pyproj import Transformer

    to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
    x0, y0, x1, y1 = bounds
    xs = [x0, x1, x0, x1, (x0 + x1) / 2, (x0 + x1) / 2, x0, x1]
    ys = [y0, y0, y1, y1, y0, y1, (y0 + y1) / 2, (y0 + y1) / 2]
    lons, lats = to_ll.transform(xs, ys)
    return (min(lons), min(lats), max(lons), max(lats))


def day_number(when: dt.date | dt.datetime) -> int:
    """Days since 2020-01-01: a compact uint16 day stamp for per-pixel state (valid until 2199)."""
    day = when.date() if isinstance(when, dt.datetime) else when
    return (day - dt.date(2020, 1, 1)).days


def from_day_number(n: int) -> dt.date:
    return dt.date(2020, 1, 1) + dt.timedelta(days=int(n))


__all__ = [
    "E84_ASSETS", "E84_COLLECTION", "GDAL_OPTIONS", "REFLECTANCE_BANDS", "WORLDCOVER_CROPLAND",
    "BlockRead", "SceneItem", "TileGrid", "day_number", "from_day_number",
    "parse_item", "read_pass_block", "read_worldcover", "reflectance", "search_scenes",
    "utm_bounds_to_lonlat", "worldcover_hrefs",
]
