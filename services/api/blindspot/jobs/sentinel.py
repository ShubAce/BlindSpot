"""Job `sentinel` (every 6 h): new clear Sentinel-2 passes -> burn scars, harvested fields, fire match.

What one run does:
1. Catalogue: search Earth Search for every S2 L2A item over Punjab and Haryana since the season
   started, and record each in s2_scene (too cloudy or almost all nodata -> `skipped`).
2. For each processing stage in priority order (the hotspot districts of HOTSPOT_DISTRICTS at 20 m,
   then the rest of Punjab, then the rest of Haryana at 40 m), for each MGRS tile: read only the
   passes this tile has not folded in yet (all of them when the stage's area on the tile changed),
   block by block (1024 x 1024 px, aligned with the COG tiles, about 150 MB of RAM per block), and
   let each pixel compare itself with its own previous clear pass (analytics.burnscar). Pixels are
   judged only inside the stage's districts, on cropland, and in the tile's own 100 km MGRS square,
   so the ~10 km overlap between tiles is never counted twice.
3. Stitch clusters cut by block edges, drop those under 0.52 ha, and replace this tile's rows for
   the passes just processed (so a rerun after a crash never duplicates a scar); a stored scar with
   the same outline and window is kept, id, verdict and names included, and only its computed
   columns are updated if they changed (a new confidence formula or rule version), so a rebuild
   writes only what changed. The harvested-but-unburnt snapshot of the tile is replaced the same
   way, matching whole rows. Rows another stage left wholly inside this stage's area on the tile (a
   district moved between stages) are deleted in the same transaction (claim_moved_rows). A scar
   deleted by any of these steps has its id forwarded to the stored scar of the same burn that
   overlaps it most (burn_cluster_forward), so its case link keeps working.
4. Match every scar against fire detections (fire_detection, filled by the fires module) and name
   its district, sub-district and nearest village from the boundary and place tables. Writes that
   can touch most of a table go in steps of their own, each followed by a VACUUM, so the database
   never needs room for a second copy of a table (Neon's free plan stops every write at 1 GB).

A run stops starting new tiles after SENTINEL_BUDGET_MIN minutes (default 50); the next run picks
up where it stopped, because each block remembers which passes it has folded in (an .npz file per
block under CACHE_DIR/sentinel). A tile's memory is committed only after its rows are in the
database, so a crash just redoes that tile.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import shutil
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import shapely
from pyproj import Transformer
from rasterio import features
from rasterio.windows import Window
from shapely.geometry import Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import column, func, insert, select, table, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from blindspot.analytics import burnscar, fires_stats
from blindspot.analytics.burnscar import RULE, BlockState, Part
from blindspot.config import get_settings
from blindspot.db import MAX_ID, STEP_ROWS, DeadRows, id_ranges, session_scope
from blindspot.geo import fill_admin
from blindspot.jobs import job
from blindspot.models.sentinel import BurnCluster, HarvestCluster, S2Scene
from blindspot.provenance import track_fetch, utcnow
from blindspot.sources import stac

log = logging.getLogger(__name__)

BLOCK_PX = 1024  # 2 x 2 internal COG tiles of the 20 m bands
MAX_CLOUD_PCT = 70.0  # skip passes whose whole tile is mostly cloud (doc section 4, step 1)
MAX_NODATA_PCT = 99.0  # a swath-edge sliver with nothing to read
# A tile whose own 100 km square reaches a stage's area by less than this is left out of that
# stage. The areas come from two boundary datasets (Survey of India states, LGD districts), whose
# difference leaves border strips of a few square metres along district edges (2026-11-23: 0.007
# km2 of tile 43RCP in 'punjab'); re-reading a whole tile for such a strip maps nothing.
MIN_STAGE_OVERLAP_M2 = 1_000_000.0
SETTLE_HOURS = 6  # wait this long after sensing, so every granule of the pass is in the catalogue
SEASON_MONTH, SEASON_DAY = 10, 1  # the paddy-residue season we monitor starts on 1 October
VILLAGE_MAX_KM = 3.0  # name the nearest village only if it is this close
MATCH_TOLERANCE_H = 3  # a detection up to 3 h after the post pass still counts as "seen" (smouldering)
PASS_READERS = 3  # passes of one block read at once (each reads its 10 bands with 12 threads)

# The districts that lead the burning year after year, as (state, district): they are processed
# first and at full 20 m resolution, the rest of each state at 40 m. Names are matched
# case-insensitively against the boundary table's districts of that state, with the spellings
# different datasets use (LGD "Ferozepur", geoBoundaries "Firozpur").
HOTSPOT_DISTRICTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("Punjab", "Sangrur"): ("sangrur",),
    ("Punjab", "Bathinda"): ("bathinda", "bhatinda"),
    ("Punjab", "Mansa"): ("mansa",),
    ("Punjab", "Barnala"): ("barnala",),
    ("Punjab", "Patiala"): ("patiala",),
    ("Punjab", "Moga"): ("moga",),
    ("Punjab", "Firozpur"): ("firozpur", "ferozepur", "firozepur", "ferozpur"),
    ("Punjab", "Ludhiana"): ("ludhiana",),
    ("Punjab", "Fatehgarh Sahib"): ("fatehgarh sahib",),
    ("Punjab", "Tarn Taran"): ("tarn taran",),
    ("Punjab", "Amritsar"): ("amritsar",),
    # Added 2026-11-23: in the 2026 season these burn as much as the districts above (NASA FIRMS
    # detections 15 Sep-22 Nov: Fazilka 1,267, 5th of all Punjab and Haryana; Sri Muktsar Sahib 892;
    # Kapurthala 618; Faridkot 567; Gurdaspur 535; Jalandhar 411; Malerkotla 393), and at 40 m a
    # pixel mixing a burnt field with its unburnt edge often fails the darkening tests, so burns that
    # are plain on the 10 m pictures went unmapped.
    ("Punjab", "Fazilka"): ("fazilka",),
    ("Punjab", "Sri Muktsar Sahib"): ("sri muktsar sahib", "muktsar", "shri muktsar sahib"),
    ("Punjab", "Kapurthala"): ("kapurthala",),
    ("Punjab", "Faridkot"): ("faridkot",),
    ("Punjab", "Gurdaspur"): ("gurdaspur",),
    ("Punjab", "Jalandhar"): ("jalandhar",),
    ("Punjab", "Malerkotla"): ("malerkotla",),
}
STATES = ("punjab", "haryana")  # each state's districts outside HOTSPOT_DISTRICTS form its 40 m stage


@dataclass(frozen=True)
class Stage:
    """One processing scope: a name, the pixel size used there, and how to find its polygon."""

    name: str
    res: int
    doc: str


STAGES = (
    Stage("hotspots", 20, "high-burning districts (HOTSPOT_DISTRICTS), 20 m (field scale: a 1 ha plot is ~25 px)"),
    Stage("punjab", 40, "rest of Punjab, 40 m COG overviews (state-scale area; ~6 px per 1 ha plot)"),
    Stage("haryana", 40, "rest of Haryana, 40 m COG overviews"),
)


def season_start(now: dt.datetime) -> dt.datetime:
    """1 October of the current paddy season (the previous year's before July)."""
    year = now.year if now.month >= 7 else now.year - 1
    return dt.datetime(year, SEASON_MONTH, SEASON_DAY, tzinfo=dt.UTC)


def budget_minutes() -> float:
    try:
        return float(os.environ.get("SENTINEL_BUDGET_MIN", "50"))
    except ValueError:
        return 50.0


def state_root() -> Path:
    return get_settings().cache_dir / "sentinel"


# --- scope polygons from the boundary table -------------------------------------------------------


def _union(session: Session, where: str, params: dict[str, Any]) -> BaseGeometry | None:
    row = session.execute(
        text(f"SELECT ST_AsGeoJSON(ST_Union(geom)) FROM boundary WHERE {where}"), params
    ).scalar()
    return shape(orjson.loads(row)) if row else None


def stage_geometries(session: Session) -> tuple[dict[str, BaseGeometry], dict[str, Any]]:
    """Each stage's area as one lon/lat geometry, built from the boundary table (never typed in).

    Returns ({stage: geometry}, notes): 'hotspots' is the union of HOTSPOT_DISTRICTS, and each of
    STATES is its state minus those districts. Empty when the boundaries have not been loaded yet."""
    notes: dict[str, Any] = {}
    aliases: dict[str, set[str]] = defaultdict(set)  # lower-case state -> district spellings
    for (state, _name), names in HOTSPOT_DISTRICTS.items():
        aliases[state.lower()].update(names)
    pieces, found = [], []
    for state, names in sorted(aliases.items()):
        where = "level = 'district' AND lower(state) = :state AND lower(name) = ANY(:names)"
        params = {"state": state, "names": sorted(names)}
        if (geom := _union(session, where, params)) is not None and not geom.is_empty:
            pieces.append(geom.buffer(0))
        found += session.execute(text(f"SELECT state || ': ' || name FROM boundary WHERE {where} ORDER BY name"),
                                 params).scalars().all()
    notes["hotspot_districts_found"] = found
    out: dict[str, BaseGeometry] = {}
    if pieces:
        out["hotspots"] = shapely.union_all(pieces).buffer(0)
    for state in STATES:
        whole = _union(session, "level = 'state' AND lower(name) = :state", {"state": state}) or _union(
            session, "level = 'district' AND lower(state) = :state", {"state": state})
        if whole is not None and not whole.is_empty:
            rest = whole.buffer(0)
            if "hotspots" in out:
                rest = rest.difference(out["hotspots"])
            out[state] = rest
    return out, notes


# --- catalogue -------------------------------------------------------------------------------------


def refresh_catalogue(
    area: BaseGeometry, start: dt.datetime, end: dt.datetime
) -> tuple[list[stac.SceneItem], dict[str, Any]]:
    """Search the STAC catalogue (outside any transaction) and upsert every item into s2_scene.
    Existing rows keep their processing status; only catalogue facts are refreshed."""
    simple = area.simplify(0.01, preserve_topology=True).buffer(0.01)
    scenes, fetch_id = stac.search_scenes(mapping(simple), start, end)
    rows = []
    for s in scenes:
        reason = None
        if s.cloud_pct is not None and s.cloud_pct > MAX_CLOUD_PCT:
            reason = f"cloud cover {s.cloud_pct:.0f}% > {MAX_CLOUD_PCT:.0f}%"
        elif s.nodata_pct is not None and s.nodata_pct >= MAX_NODATA_PCT:
            reason = f"nodata {s.nodata_pct:.1f}% (swath edge)"
        rows.append({
            "item_id": s.item_id, "collection": s.collection, "datetime": s.datetime,
            "obs_date": s.obs_date, "mgrs_tile": s.mgrs_tile, "platform": s.platform,
            "relative_orbit": s.relative_orbit, "cloud_pct": s.cloud_pct, "nodata_pct": s.nodata_pct,
            "bbox": list(s.bbox), "geom": f"SRID=4326;{shape(s.geometry).wkt}",
            "status": "skipped" if reason else "catalogued", "error": reason, "fetch_id": fetch_id,
        })
    if rows:
        stmt = pg_insert(S2Scene).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["item_id"],
            set_={"cloud_pct": stmt.excluded.cloud_pct, "nodata_pct": stmt.excluded.nodata_pct,
                  "geom": stmt.excluded.geom, "bbox": stmt.excluded.bbox},
        )
        with session_scope() as session:
            session.execute(stmt)
    skipped = sum(r["status"] == "skipped" for r in rows)
    return scenes, {"items": len(scenes), "skipped": skipped, "fetch_id": fetch_id}


# --- one tile ------------------------------------------------------------------------------------


@dataclass
class TileResult:
    tile: str
    stage: str
    passes: list[str] = field(default_factory=list)  # ISO dates folded in this run
    blocks: int = 0
    windows_read: int = 0
    burn_rows: list[dict[str, Any]] = field(default_factory=list)
    harvest_rows: list[dict[str, Any]] = field(default_factory=list)
    post_days: set[int] = field(default_factory=set)
    processed_blocks: set[str] = field(default_factory=set)  # blocks that folded in new passes
    rebuilt: bool = False
    filtered_clusters: int = 0  # burn clusters dropped by the look-alike filter (BurnRule.keeps)
    gate_failures: list[str] = field(default_factory=list)
    hazy_passes: set[str] = field(default_factory=set)
    item_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    seconds: float = 0.0
    # The stage's area inside the tile's own square (lon/lat WKT): rows of other stages lying wholly
    # inside it were moved here (a district changed stage), so storing this tile deletes them.
    area_wkt: str = ""


def _tile_dir(stage: Stage, tile: str) -> Path:
    return state_root() / RULE.pixel_version / stage.name / f"{tile}_{stage.res}m"


def _area_key(area_utm: BaseGeometry) -> str:
    """A fingerprint of a stage's area on a tile (to the metre), kept beside the tile's memory."""
    exact = shapely.normalize(shapely.set_precision(area_utm, 1.0))
    return hashlib.sha1(shapely.to_wkb(exact)).hexdigest()


def _cropland(grid: stac.TileGrid, window: Window, hrefs: list[str]) -> np.ndarray:
    """WorldCover cropland mask for one block, cached on disk (the 2021 map never changes)."""
    path = state_root() / "worldcover" / f"{grid.tile}_{grid.res}m" / f"r{window.row_off}_c{window.col_off}.npy"
    if path.exists():
        return np.load(path)
    classes = stac.read_worldcover(hrefs, grid.epsg, grid.window_transform(window), (window.height, window.width))
    mask = classes == stac.WORLDCOVER_CROPLAND
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, mask)
    return mask


def _to_lonlat(geom: BaseGeometry, epsg: int) -> BaseGeometry:
    to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
    return shapely.transform(geom, lambda xy: np.column_stack(to_ll.transform(xy[:, 0], xy[:, 1])))


def _from_lonlat(geom: BaseGeometry, epsg: int) -> BaseGeometry:
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    return shapely.transform(geom, lambda xy: np.column_stack(to_utm.transform(xy[:, 0], xy[:, 1])))


def _clean_polygon(geom: BaseGeometry, res: int) -> Polygon | None:
    """Simplify a pixel outline (half a pixel tolerance) and make sure it is one valid polygon."""
    g = geom.simplify(res / 2, preserve_topology=True)
    if not g.is_valid:
        g = shapely.make_valid(g)
    if g.geom_type != "Polygon":
        polys = [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon"]
        if not polys:
            return None
        g = max(polys, key=lambda p: p.area)
    return g if not g.is_empty else None


def _ewkt(geom: BaseGeometry) -> str:
    return f"SRID=4326;{shapely.to_wkt(geom, rounding_precision=6)}"


def _anchor(poly: Polygon) -> BaseGeometry:
    c = poly.centroid
    return c if poly.contains(c) else poly.representative_point()


def process_tile(
    stage: Stage,
    area_ll: BaseGeometry,
    tile: str,
    scenes: list[stac.SceneItem],
    now: dt.datetime,
) -> TileResult | None:
    """Fold this tile's new passes into its block memory and build its new rows (not yet stored).

    Returns None when nothing is new. The new block memory is written to a `.next` folder that the
    caller swaps in after the database commit."""
    t0 = time.perf_counter()
    result = TileResult(tile=tile, stage=stage.name)
    passes: dict[int, list[stac.SceneItem]] = defaultdict(list)
    for s in scenes:
        if s.datetime <= now - dt.timedelta(hours=SETTLE_HOURS):
            passes[stac.day_number(s.datetime)].append(s)
    if not passes:
        return None
    grid = stac.TileGrid.from_scene(scenes[0], stage.res)
    area_utm = _from_lonlat(area_ll, grid.epsg).intersection(shapely.box(*grid.core))
    if area_utm.is_empty:
        return None
    tile_dir = _tile_dir(stage, tile)
    next_dir = tile_dir.with_name(tile_dir.name + ".next")
    if next_dir.exists():
        shutil.rmtree(next_dir)
    area_key = _area_key(area_utm)
    key_path = tile_dir / "area.key"
    if tile_dir.exists() and (not key_path.exists() or key_path.read_text() != area_key):
        # The stage's area on this tile changed (a district moved between stages): the memory has
        # no history for pixels new to the area, and only a rebuild replaces the rows of the old
        # area for every window, so the tile is rebuilt from all its passes.
        shutil.rmtree(tile_dir)
    result.area_wkt = shapely.to_wkt(_to_lonlat(area_utm, grid.epsg), rounding_precision=7)
    registry_path = tile_dir / "items.json"
    registry: list[str] = orjson.loads(registry_path.read_bytes()) if registry_path.exists() else []
    result.rebuilt = not tile_dir.exists()
    windows = [w for w in grid.blocks(BLOCK_PX) if area_utm.intersects(shapely.box(*grid.window_bounds(w)))]
    sensing = {s.item_id: s.datetime for s in scenes}
    pass_days = sorted(passes)
    burn_parts: dict[int, list[Part]] = defaultdict(list)  # post day -> parts
    harvest_parts: list[Part] = []
    newest_day = pass_days[-1]
    any_new = False
    # Each item's data footprint in the tile CRS: a swath-edge pass that misses a block is not read.
    footprints = {s.item_id: _from_lonlat(shape(s.geometry), grid.epsg) for s in scenes}
    reader = ThreadPoolExecutor(max_workers=PASS_READERS)  # a block's passes are read concurrently
    try:
        for window in windows:
            any_new |= _process_block(
                window, grid, stage, tile_dir, next_dir, area_utm, passes, pass_days, footprints,
                registry, newest_day, reader, result, burn_parts, harvest_parts,
                lambda: worldcover_cache(grid),
            )
    finally:
        reader.shutdown(wait=True, cancel_futures=True)

    if not any_new:
        shutil.rmtree(next_dir, ignore_errors=True)
        return None
    result = _finish_tile(stage, tile, grid, scenes, passes, pass_days, registry, sensing, next_dir,
                          burn_parts, harvest_parts, result, t0)
    (next_dir / "area.key").write_text(area_key)
    return result


_worldcover_hrefs: dict[str, tuple[float, list[str]]] = {}
SIGNATURE_REUSE_S = 30 * 60  # Planetary Computer signatures last about 45 min; re-sign after 30


def worldcover_cache(grid: stac.TileGrid) -> list[str]:
    """Signed WorldCover URLs for a tile, re-signed when older than 30 min (cropland masks are cached
    on disk after the first run, so this is rarely called at all)."""
    signed_at, hrefs = _worldcover_hrefs.get(grid.tile, (0.0, []))
    if not hrefs or time.monotonic() - signed_at > SIGNATURE_REUSE_S:
        hrefs, _ = stac.worldcover_hrefs(stac.utm_bounds_to_lonlat(grid.epsg, grid.core))
        _worldcover_hrefs[grid.tile] = (time.monotonic(), hrefs)
    return hrefs


def _process_block(
    window: Window,
    grid: stac.TileGrid,
    stage: Stage,
    tile_dir: Path,
    next_dir: Path,
    area_utm: BaseGeometry,
    passes: dict[int, list[stac.SceneItem]],
    pass_days: list[int],
    footprints: dict[str, BaseGeometry],
    registry: list[str],
    newest_day: int,
    reader: ThreadPoolExecutor,
    result: TileResult,
    burn_parts: dict[int, list[Part]],
    harvest_parts: list[Part],
    worldcover: Any,
) -> bool:
    """Fold one block's new passes into its memory; collect its burn and harvest clusters.
    Returns True when the block had passes to fold in."""
    state_path = tile_dir / f"r{window.row_off}_c{window.col_off}.npz"
    state = BlockState.load(state_path, (window.height, window.width))
    # Passes are folded in date order; one that turns up later than a newer pass is ignored.
    todo = [d for d in pass_days if d > max(state.applied_days, default=0)]
    bounds = grid.window_bounds(window)
    block_shape = (window.height, window.width)
    scope = features.rasterize(
        [(area_utm, 1)], out_shape=block_shape, transform=grid.window_transform(window), dtype=np.uint8,
    ).astype(bool) & grid.core_mask(window)
    if todo and scope.any():
        scope &= _cropland(grid, window, worldcover())
    box = shapely.box(*bounds)
    futures = {}
    if scope.any():
        for day in todo:
            items = [s for s in passes[day] if footprints[s.item_id].intersects(box)]
            if items:
                futures[day] = (items, reader.submit(stac.read_pass_block, items, bounds, block_shape))
    try:
        for day in todo:
            if day not in futures:  # no data over this block, or nothing in scope here
                state.applied_days.append(day)
                continue
            items, future = futures.pop(day)
            read = future.result()
            result.windows_read += read.windows_read
            by_id = {s.item_id: s for s in items}
            first = by_id[read.items[0]]
            refl = {b: stac.reflectance(read.dn[b], first.scale[b], first.offset[b]) for b in stac.REFLECTANCE_BANDS}
            outcome = burnscar.apply_pass(
                state, refl, read.dn["SCL"], read.items, read.item_index, day, scope, registry,
                res_m=stage.res)
            state.applied_days.append(day)
            _note_outcome(result, read.items, outcome, day, window)
            if outcome.used and outcome.burn is not None and outcome.burn.any():
                ev = {k: np.where(outcome.burn, v, 0).astype(np.float32) for k, v in outcome.evidence.items()}
                tags = {
                    "pre_item": outcome.pre_item, "post_item": outcome.post_item,
                    "hazy": np.full(outcome.burn.shape, int(outcome.hazy), np.uint16),
                }
                burn_parts[day].extend(burnscar.extract_parts(
                    outcome.burn, outcome.pre_day, grid.window_transform(window), ev, tags))
            del refl, read, outcome
    finally:
        for _items, future in futures.values():  # an error above: drop reads still queued
            future.cancel()
    # Harvest snapshot from the (updated) memory, every run.
    if scope.any():
        cand = burnscar.harvest_candidates(state, scope, newest_day)
        if cand.any():
            a = state.arrays
            harvest_parts.extend(burnscar.extract_parts(
                cand, a["harv_before"], grid.window_transform(window),
                {"ndvi_last": np.nan_to_num(a["last_ndvi"].astype(np.float32))},
                {"harv_after": a["harv_after"], "last_day": a["last_day"], "last_item": a["last_item"]},
            ))
    if todo:
        state.save(next_dir / state_path.name)  # unchanged blocks are carried over on commit
        result.blocks += 1
        result.processed_blocks.add(_block_id(window))
    return bool(todo)


def _block_id(window: Window) -> str:
    return f"r{window.row_off}_c{window.col_off}"


def _anchor_block(grid: stac.TileGrid, x: float, y: float) -> str:
    """The processing block that holds a point of the tile CRS."""
    col = int((x - grid.x0) // grid.res) // BLOCK_PX * BLOCK_PX
    row = int((grid.y0 - y) // grid.res) // BLOCK_PX * BLOCK_PX
    return f"r{row}_c{col}"


def _note_outcome(result: TileResult, item_ids: list[str], outcome: burnscar.PassResult, day: int,
                  window: Window) -> None:
    """Per-item bookkeeping for s2_scene (blocks used / failed / hazy) and the run summary."""
    iso = stac.from_day_number(day).isoformat()
    gate_failed = (not outcome.used) and bool(outcome.reason) and "implausible" in (outcome.reason or "")
    for item_id in item_ids:
        st = result.item_stats.setdefault(item_id, {"blocks_used": 0, "blocks_failed": 0, "hazy_blocks": 0})
        st["blocks_used" if outcome.used else "blocks_failed"] += 1
        st["hazy_blocks"] += int(outcome.hazy)
        if gate_failed:
            st["error"] = outcome.reason
    if gate_failed:
        result.gate_failures.append(f"{iso} r{window.row_off}c{window.col_off}: {outcome.reason}")
    if outcome.used:
        result.post_days.add(day)
        if outcome.hazy:
            result.hazy_passes.add(iso)


def _finish_tile(
    stage: Stage,
    tile: str,
    grid: stac.TileGrid,
    scenes: list[stac.SceneItem],
    passes: dict[int, list[stac.SceneItem]],
    pass_days: list[int],
    registry: list[str],
    sensing: dict[str, dt.datetime],
    next_dir: Path,
    burn_parts: dict[int, list[Part]],
    harvest_parts: list[Part],
    result: TileResult,
    t0: float,
) -> TileResult:
    """Stitch block pieces, drop small clusters and turn the rest into database rows."""
    next_dir.mkdir(parents=True, exist_ok=True)
    (next_dir / "items.json").write_bytes(orjson.dumps(registry))
    result.passes = [stac.from_day_number(d).isoformat() for d in pass_days if d in result.post_days]
    min_px = RULE.min_px(stage.res)
    collection = scenes[0].collection
    for post_day, parts in burn_parts.items():
        for part in burnscar.iter_large(burnscar.merge_parts(parts), min_px):
            poly = _clean_polygon(part.geom, stage.res)
            if poly is None:
                continue
            pre_id = registry[part.mode("pre_item") or 0]
            post_id = registry[part.mode("post_item") or 0]
            after = sensing.get(pre_id) or _pass_time(passes, part.key, sensing)
            before = sensing.get(post_id) or _pass_time(passes, post_day, sensing)
            gap = (before - after).total_seconds() / 86400
            hazy = bool(part.mode("hazy"))
            means = {k: part.mean(k) for k in ("d_b8a", "d_b11", "d_b12", "dnbr", "nbr_post",
                                                "bais2_post", "tbi_post", "ndvi_pre", "ndvi_post")}
            if not RULE.keeps(means["nbr_post"]):
                result.filtered_clusters += 1  # water / cloud-shadow look-alike (BurnRule.keeps)
                continue
            ll = _to_lonlat(poly, grid.epsg)
            anchor_utm = _anchor(poly)
            result.burn_rows.append({
                # Area from the pixel count (exact), not from the simplified outline.
                "geom": _ewkt(ll), "centroid": _ewkt(_anchor(ll)),
                "area_ha": round(part.n_px * stage.res**2 / 10_000, 3),
                "n_px": part.n_px, "res_m": stage.res, "mgrs_tile": tile, "scope": stage.name,
                "block": _anchor_block(grid, anchor_utm.x, anchor_utm.y),
                "burn_after": after, "burn_before": before, "gap_days": round(gap, 2),
                "scene_pre": pre_id, "scene_post": post_id, "collection": collection, "hazy": hazy,
                **{k: (round(v, 4) if v is not None else None) for k, v in means.items()},
                "confidence": burnscar.confidence(
                    means["tbi_post"] if means["tbi_post"] is not None else RULE.tbi_max,
                    means["d_b12"] if means["d_b12"] is not None else RULE.d_b12,
                    part.n_px, min_px, hazy=hazy, gap_days=gap),
                "model_version": RULE.version,
            })
    for part in burnscar.iter_large(burnscar.merge_parts(harvest_parts), min_px):
        poly = _clean_polygon(part.geom, stage.res)
        if poly is None:
            continue
        ll = _to_lonlat(poly, grid.epsg)
        result.harvest_rows.append({
            "geom": _ewkt(ll), "centroid": _ewkt(_anchor(ll)),
            "area_ha": round(part.n_px * stage.res**2 / 10_000, 3), "n_px": part.n_px,
            "res_m": stage.res, "mgrs_tile": tile, "scope": stage.name,
            "harvested_after": _pass_time(passes, part.mode("harv_after") or part.key, sensing),
            "harvested_before": _pass_time(passes, part.key, sensing),
            "last_seen_unburnt": _pass_time(passes, part.max_tag("last_day") or part.key, sensing),
            "scene_last": registry[part.mode("last_item") or 0], "collection": collection,
            "ndvi_last": round(part.mean("ndvi_last") or 0.0, 4),
            "model_version": RULE.version,
        })
    result.seconds = round(time.perf_counter() - t0, 1)
    return result


def _pass_time(passes: dict[int, list[stac.SceneItem]], day: int, sensing: dict[str, dt.datetime]) -> dt.datetime:
    """Sensing time of a tile's pass on `day` (its least-nodata item), or midnight UTC if unknown."""
    items = passes.get(day)
    if items:
        best = min(items, key=lambda s: s.nodata_pct if s.nodata_pct is not None else 100.0)
        return sensing.get(best.item_id, best.datetime)
    return dt.datetime.combine(stac.from_day_number(day), dt.time(), dt.UTC)


def store_tile(result: TileResult, fetch_id: int | None) -> dict[str, dict[str, int]]:
    """Bring this tile's rows for the passes just processed in line with what was computed (rows
    that are the same scar keep their id, verdict and names, see _replace_rows), then swap in its
    new memory. Returns _replace_rows' counts per table."""
    stage_name, tile = result.stage, result.tile
    tile_sql = "t.mgrs_tile = :tile AND t.scope = :scope"
    tile_params: dict[str, Any] = {"tile": tile, "scope": stage_name}
    burn_scope: str | None = None
    burn_params = dict(tile_params)
    if result.rebuilt:
        burn_scope = tile_sql
    elif result.post_days:
        # Only rows of blocks that were reprocessed for those passes; others stay as they are.
        burn_scope = (f"{tile_sql} AND date(t.burn_before) = ANY(CAST(:days AS date[]))"
                      " AND t.block = ANY(CAST(:blocks AS text[]))")
        burn_params.update(days=[stac.from_day_number(d) for d in sorted(result.post_days)],
                           blocks=sorted(result.processed_blocks))
    with session_scope() as session:
        stored = {
            "burn_cluster": _replace_rows(session, BurnCluster, burn_scope, burn_params,
                                          [{**r, "fetch_id": fetch_id} for r in result.burn_rows]),
            # The harvested-but-unburnt snapshot of the tile is always computed whole.
            "harvest_cluster": _replace_rows(session, HarvestCluster, tile_sql, tile_params,
                                             [{**r, "fetch_id": fetch_id} for r in result.harvest_rows]),
        }
        claimed = claim_moved_rows(session, tile, stage_name, result.area_wkt)
        for name, counts in stored.items():
            counts["claimed"] = claimed.get(name, 0)
        stored["burn_cluster"]["forwarded"] += claimed.get("forwarded", 0)
        current = dict(session.execute(
            select(S2Scene.item_id, S2Scene.stats).where(S2Scene.item_id.in_(list(result.item_stats)))
        ).all())
        for item_id, st in result.item_stats.items():
            # "failed" only when every block it was read for rejected it at the plausibility gate.
            status = "failed" if (not st["blocks_used"] and st.get("error")) else "processed"
            stats = {**(current.get(item_id) or {}), stage_name: st}
            session.execute(update(S2Scene).where(S2Scene.item_id == item_id).values(
                status=status, processed_at=utcnow(), error=st.get("error"), stats=stats))
    tile_dir = _tile_dir(next(s for s in STAGES if s.name == stage_name), tile)
    next_dir = tile_dir.with_name(tile_dir.name + ".next")
    old_dir = tile_dir.with_name(tile_dir.name + ".old")
    if old_dir.exists():
        shutil.rmtree(old_dir)
    if tile_dir.exists():
        # Untouched blocks (outside this run's work) carry over; changed ones come from .next.
        for path in tile_dir.glob("*.npz"):
            if not (next_dir / path.name).exists():
                shutil.copy2(path, next_dir / path.name)
        tile_dir.rename(old_dir)
    next_dir.rename(tile_dir)
    shutil.rmtree(old_dir, ignore_errors=True)
    return stored


def apply_cluster_filter(session: Session) -> dict[str, int]:
    """Bring stored rows in line with the current cluster-level filter (BurnRule.keeps).

    Rows from an older version whose pixel rule equals today's (RULE.pixel_version) are re-judged
    from their stored mean post NBR: look-alikes are deleted, the rest relabelled to RULE.version.
    Idempotent; cheap; never touches imagery. Rows from any other pixel rule are left alone."""
    older = [RULE.pixel_version] if RULE.pixel_version != RULE.version else []
    if not older:
        return {"deleted": 0, "relabelled": 0}
    deleted = session.execute(text(
        "DELETE FROM burn_cluster WHERE model_version = ANY(:old) AND nbr_post >= :mx"),
        {"old": older, "mx": RULE.nbr_post_max}).rowcount
    relabelled = session.execute(text(
        "UPDATE burn_cluster SET model_version = :v WHERE model_version = ANY(:old)"),
        {"v": RULE.version, "old": older}).rowcount
    return {"deleted": deleted, "relabelled": relabelled}


# --- fire matching and names ----------------------------------------------------------------------

# A scar's window is "covered" when every UTC day from its clear pass to 3 h after its burnt pass is
# one of :days (days on which fire records exist for every FIRMS source we hold).
_COVERED = f"""
    NOT EXISTS (
        SELECT 1 FROM generate_series(b.burn_after::date,
                                      (b.burn_before + interval '{MATCH_TOLERANCE_H} hours')::date,
                                      interval '1 day') AS d
        WHERE NOT (d::date = ANY(CAST(:days AS date[])))
    )
"""
_MATCH_SQL = text(
    f"""
    WITH m AS (
        SELECT b.id,
               count(f.id) FILTER (WHERE f.acq_utc >= b.burn_after
                     AND f.acq_utc <= b.burn_before + interval '{MATCH_TOLERANCE_H} hours'
                     AND ST_DWithin(b.geom::geography, f.geom::geography,
                         GREATEST(1000.0, 500.0 * sqrt(coalesce(f.scan, 0)^2 + coalesce(f.track, 0)^2) + 200.0))
               ) AS n,
               min(ST_Distance(b.geom::geography, f.geom::geography)) FILTER (
                     WHERE f.acq_utc >= b.burn_after
                       AND f.acq_utc <= b.burn_before + interval '{MATCH_TOLERANCE_H} hours') AS nearest
        FROM burn_cluster b
        LEFT JOIN fire_detection f
               ON f.geom && ST_Expand(b.geom, 0.05)
              AND f.acq_utc >= b.burn_after - interval '1 day'
              AND f.acq_utc <= b.burn_before + interval '1 day'
        WHERE b.id >= :lo AND b.id < :hi AND {_COVERED}
        GROUP BY b.id
    )
    UPDATE burn_cluster b
       SET fire_matches = m.n, unseen = (m.n = 0), nearest_fire_m = round(m.nearest::numeric, 1),
           matched_at = now()
      FROM m
     WHERE m.id = b.id
       AND (b.fire_matches IS DISTINCT FROM m.n OR b.unseen IS DISTINCT FROM (m.n = 0)
            OR b.nearest_fire_m IS DISTINCT FROM round(m.nearest::numeric, 1)::float8)
    """
)
_UNCOVERED_SQL = text(
    f"""
    UPDATE burn_cluster b SET fire_matches = NULL, unseen = NULL, nearest_fire_m = NULL, matched_at = now()
     WHERE b.id >= :lo AND b.id < :hi AND NOT ({_COVERED}) AND (b.unseen IS NOT NULL OR b.matched_at IS NULL)
    """
)


def covered_days(session: Session) -> tuple[list, str]:
    """UTC days for which we hold fire records from every FIRMS source that published on that day.

    Uses the fires module's fire_coverage table (one row per source and day fetched from the FIRMS
    area API). A source that has stopped publishing is not needed after its last day
    (fires_stats.ended_sources: VIIRS_SNPP_NRT after 2 Nov 2026, which had left every later scar
    without a verdict). Without the table, falls back to every day between the first and last
    detection stored."""
    if session.execute(text("SELECT to_regclass('public.fire_coverage')")).scalar() is not None:
        sources = session.execute(text("SELECT DISTINCT source FROM fire_coverage")).scalars().all()
        ended = fires_stats.ended_sources(session, sources)
        days = sorted(fires_stats.covered_utc_days(session, sources, ended, complete_only=False))
        if days:
            stopped = "".join(f"; NASA lists no {s} after {d}" for s, d in sorted(ended.items()))
            return days, f"fire_coverage: days fetched for every FIRMS source NASA published{stopped}"
    first, last = session.execute(text("SELECT min(acq_utc)::date, max(acq_utc)::date FROM fire_detection")).one()
    if first is None:
        return [], "no fire records"
    n = (last - first).days + 1
    return [first + dt.timedelta(days=i) for i in range(n)], "first..last stored detection (no fire_coverage)"


def match_fires(session: Session) -> dict[str, Any]:
    """Count fire detections that explain each scar and set `unseen`, in one transaction.

    A detection explains a scar when it lies within max(1 km, its own pixel half-diagonal + 200 m)
    of the scar polygon (SYNTHESIS section 2.5: VIIRS 465-760 m, MODIS ~1-2.8 km; never less than
    1 km, so "unseen" stays conservative) and was acquired between the clear pass before the burn
    and 3 h after the pass that shows it. A scar gets a verdict only when fire records cover every
    day of its window for every FIRMS source; otherwise unseen stays NULL: no data is not the same as
    no fire. Re-run every job run, so verdicts follow late-arriving fire data, but only scars whose
    verdict changes are rewritten (an UPDATE writes a whole new row version, and rewriting all of
    them every run kept the table twice its size), so matched_at is when the current verdict was set.
    The job runs it in steps: match_fires_in_batches."""
    records = _fire_records(session)
    if isinstance(records, str):
        return {"skipped": records}
    matched, unknown = _match_range(session, records[1], 0, MAX_ID)
    return _match_summary(records, matched, unknown)


def match_fires_in_batches(rows: int = STEP_ROWS, dead: DeadRows | None = None) -> dict[str, Any]:
    """match_fires in steps of `rows` scars, each its own transaction, counting the rows it rewrites
    into `dead` (a DeadRows of burn_cluster; by default its own, finished here), which vacuums as
    they pile up, so each step reuses the room of the old row versions before it.

    Every verdict can change at once: all of a rebuilt tile's new scars arrive without one, and a
    FIRMS backfill can complete many days. In one transaction that wrote a second copy of the whole
    table (277k scars, ~250 MB with its indexes), more than Neon's 1 GB cap had room for: 8-12 Nov
    2026 every run failed there and every scar was left without a verdict."""
    with session_scope() as session:
        records = _fire_records(session)
        ranges = id_ranges(session, "burn_cluster", rows)
    if isinstance(records, str):
        return {"skipped": records}
    own = dead is None
    dead = dead or DeadRows("burn_cluster")
    matched = unknown = 0
    for lo, hi in ranges:
        with session_scope() as session:
            step = _match_range(session, records[1], lo, hi)
        matched, unknown = matched + step[0], unknown + step[1]
        dead.add(sum(step))
    if own:
        dead.finish()
    return {**_match_summary(records, matched, unknown), "steps": len(ranges), "vacuums": dead.vacuums}


def _fire_records(session: Session) -> tuple[int, list, str] | str:
    """(fire records held, covered days, how they were found), or why there is nothing to match."""
    if session.execute(text("SELECT to_regclass('public.fire_detection')")).scalar() is None:
        return "fire_detection table does not exist yet (fires module)"
    n_fires = session.execute(text("SELECT count(*) FROM fire_detection")).scalar()
    if not n_fires:
        return "fire_detection is empty"
    return (n_fires, *covered_days(session))


def _match_range(session: Session, days: list, lo: int, hi: int) -> tuple[int, int]:
    """Set the verdicts of the scars with ids in [lo, hi): (rows rewritten with a verdict, without)."""
    ids = {"days": days, "lo": lo, "hi": hi}
    return session.execute(_MATCH_SQL, ids).rowcount, session.execute(_UNCOVERED_SQL, ids).rowcount


def _match_summary(records: tuple[int, list, str], matched: int, unknown: int) -> dict[str, Any]:
    n_fires, days, basis = records
    return {"fire_records": n_fires, "covered_days": [str(days[0]), str(days[-1]), len(days)] if days else [],
            "coverage_basis": basis, "verdicts_changed": matched, "verdicts_withdrawn_no_coverage": unknown}


_SUBDISTRICT_SQL = """
    UPDATE {table} t SET subdistrict = b.name
      FROM boundary b
     WHERE t.subdistrict IS NULL AND b.level = 'subdistrict' AND ST_Intersects(b.geom, t.centroid)
"""
_VILLAGE_SQL = """
    WITH v AS (
        SELECT t.id, p.name, p.km
          FROM {table} t
          CROSS JOIN LATERAL (
              SELECT name, ST_Distance(geom::geography, t.centroid::geography) / 1000.0 AS km
                FROM place WHERE kind IN ('village', 'town')
               ORDER BY geom <-> t.centroid LIMIT 1
          ) p
         WHERE t.village IS NULL
    )
    UPDATE {table} t SET village = v.name, village_km = round(v.km::numeric, 2)
      FROM v WHERE v.id = t.id AND v.km <= :max_km
"""


def _name_places(session: Session, name: str) -> dict[str, int]:
    """District/state (via geo.fill_admin), sub-district and nearest village for the unnamed rows of
    table `name`. A row that has its names, or has none within reach, is not rewritten."""
    return {
        "district": fill_admin(session, name),
        "subdistrict": session.execute(text(_SUBDISTRICT_SQL.format(table=name))).rowcount,
        "village": session.execute(text(_VILLAGE_SQL.format(table=name)), {"max_km": VILLAGE_MAX_KM}).rowcount,
    }


# What makes a stored row the same thing as a newly computed one. A scar is its tile, stage, burn
# window and outline: everything else on the row (confidence, the evidence means, model_version) is
# computed from those pixels by rules that can change, and its fire verdict and place names depend
# only on the outline and the window. Tables without an entry match on their whole content.
IDENTITY: dict[str, tuple[str, ...]] = {
    "burn_cluster": ("mgrs_tile", "scope", "burn_after", "burn_before", "geom"),
}


def _replace_rows(session: Session, model: type[BurnCluster | HarvestCluster], scope: str | None,
                  params: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, int]:
    """Make the stored rows inside `scope` (an SQL condition on alias `t` with `params`; None: no
    stored row is replaced) equal to `rows`, writing only what changed.

    A stored row inside the scope that is the same thing as a new row (IDENTITY: for a scar its
    tile, stage, window and outline; otherwise every column the run computes, i.e. every key of
    `rows` but fetch_id) keeps its id, so links to it keep working, and what later steps wrote onto
    it (a scar's fire verdict and place names; a harvested field's place names and risk score). If
    the columns the run computes came out the same, the row is not touched; if not (a new
    confidence formula or rule version), those columns and fetch_id are updated in place, and
    created_at keeps the time the row was first stored.
    Stored rows that nothing matched are deleted; new rows that nothing matched are named in a
    temporary copy of the table (same columns, defaults and id sequence), so each is written once,
    then inserted. (Naming rows after they landed wrote every new row three more times.)

    Why: GitHub's runner starts every run without the block memory, so each tile is rebuilt from
    all of the season's passes, and replacing all its rows rewrote every scar and harvested field
    daily (~277k and ~264k by November), each again for its fire verdict and its risk score. The
    dead copies needed more room than Neon's 1 GB cap left: the runs of 8-12 Nov 2026 all failed.
    Matched rows also keep their ids, so a case link stays valid across daily runs and rule
    changes that leave a scar's outline and window alone."""
    real = model.__table__
    stage = table(f"new_{real.name}", *(column(c.name, c.type) for c in real.columns))
    out = {"computed": len(rows), "kept": 0, "updated": 0, "deleted": 0, "inserted": 0}
    forward = real.name == "burn_cluster"  # deleted scars are kept aside, then forwarded (forward_gone)
    if forward:
        out["forwarded"] = 0
        _gone_table(session)
    gone_cols = GONE_COLS if forward else "1"
    saved = f"saved AS (INSERT INTO {GONE_TABLE} SELECT * FROM gone RETURNING 1)," if forward else ""
    session.execute(text(f"CREATE TEMP TABLE {stage.name} (LIKE {real.name} INCLUDING DEFAULTS) ON COMMIT DROP"))
    if rows:
        session.execute(insert(stage), rows)
    if scope is not None and rows:
        computed = [name for name in rows[0] if name != "fetch_id"]
        identity = IDENTITY.get(real.name, tuple(computed))
        rest = [name for name in computed if name not in identity]  # equal identity: compare the rest
        # Geometry as its EWKB hex, floats as exact text.
        isig = f"md5(ROW({', '.join(f't.{name}' for name in identity)})::text)"
        csig = f"md5(ROW({', '.join(f't.{name}' for name in rest)})::text)"
        assign = ", ".join(f"{name} = s.{name}" for name in rows[0] if name not in identity)
        update = (f"UPDATE {real.name} t SET {assign} FROM pair p JOIN {stage.name} s ON s.id = p.new_id "
                  "WHERE t.id = p.old_id AND NOT p.same RETURNING 1") if assign else "SELECT 1 WHERE false"
        # Pair new and stored rows of equal identity one to one (row_number among equals), update the
        # pairs whose computed columns differ, delete the stored rows nothing paired and the new rows
        # already stored, all in one statement (its parts see the tables as they were before it, and
        # each runs to completion whether or not the final SELECT reads it).
        out["deleted"], out["kept"], out["updated"] = session.execute(text(f"""
            WITH n AS (SELECT id, isig, csig, row_number() OVER (PARTITION BY isig ORDER BY id) AS rn
                       FROM (SELECT t.id, {isig} AS isig, {csig} AS csig FROM {stage.name} t) s),
                 o AS (SELECT id, isig, csig, row_number() OVER (PARTITION BY isig ORDER BY id) AS rn
                       FROM (SELECT t.id, {isig} AS isig, {csig} AS csig FROM {real.name} t WHERE {scope}) s),
                 pair AS (SELECT n.id AS new_id, o.id AS old_id, n.csig = o.csig AS same
                          FROM n JOIN o USING (isig, rn)),
                 gone AS (DELETE FROM {real.name} t USING o WHERE t.id = o.id
                           AND NOT EXISTS (SELECT 1 FROM pair p WHERE p.old_id = o.id) RETURNING {gone_cols}),
                 {saved}
                 changed AS ({update}),
                 taken AS (DELETE FROM {stage.name} t USING pair p WHERE t.id = p.new_id RETURNING 1)
            SELECT (SELECT count(*) FROM gone), (SELECT count(*) FROM pair WHERE same),
                   (SELECT count(*) FROM changed)
        """), params).one()
    elif scope is not None:
        delete = f"DELETE FROM {real.name} t WHERE {scope}"
        out["deleted"] = (_delete_keeping_gone(session, delete, params) if forward
                          else session.execute(text(delete), params).rowcount)
    if out["computed"] > out["kept"] + out["updated"]:  # some new rows matched nothing stored
        _name_places(session, stage.name)
        out["inserted"] = session.execute(text(f"INSERT INTO {real.name} SELECT * FROM {stage.name}")).rowcount
    session.execute(text(f"DROP TABLE {stage.name}"))
    if forward and out["deleted"]:
        out["forwarded"] = forward_gone(session)
    return out


# --- case links that survive a replaced scar --------------------------------------------------------

GONE_TABLE = "gone_scar"  # scars deleted in this transaction, waiting to be forwarded
GONE_COLS = "t.id, t.geom, t.burn_before"  # its columns, in order, as a DELETE FROM burn_cluster t returns them
# A deleted scar's successor: the stored scar of the same burn whose outline overlaps it most.
# - First the scars of the same post pass day (any overlap).
# - Failing that, a scar of another post pass day within RULE.rearm_days that covers at least
#   FORWARD_MOVED_SHARE of the old outline: the same burn seen on another pass. A rule change can
#   move a burn to an earlier pass, e.g. s2burn-rules-4h finds on a hazy pass what rules-2 only saw
#   on the next clear one. A pixel cannot burn twice within rearm_days, so that is the same burn.
# ST_MakeValid: an outline rounded to 6 decimals can self-touch, and GEOS would reject it in an
# intersection.
FORWARD_MOVED_SHARE = 0.5
_FORWARD_SQL = text(f"""
    INSERT INTO burn_cluster_forward (old_id, new_id, overlap_share, forwarded_at)
    SELECT g.id, s.id, s.share, now()
      FROM {GONE_TABLE} g
      CROSS JOIN LATERAL (
          SELECT c.id, c.share FROM (
              SELECT b.id, ST_Area(ST_Intersection(ST_MakeValid(b.geom), ST_MakeValid(g.geom)))
                           / NULLIF(ST_Area(g.geom), 0) AS share,
                     abs(b.burn_before::date - g.burn_before::date) AS days_apart
                FROM burn_cluster b
               WHERE b.geom && g.geom
                 AND abs(b.burn_before::date - g.burn_before::date) <= {int(RULE.rearm_days)}) c
           WHERE c.share > 0 AND (c.days_apart = 0 OR c.share >= {FORWARD_MOVED_SHARE})
           ORDER BY c.days_apart > 0, c.share DESC, c.days_apart, c.id
           LIMIT 1) s
    ON CONFLICT (old_id) DO UPDATE SET new_id = EXCLUDED.new_id, overlap_share = EXCLUDED.overlap_share,
                                       forwarded_at = EXCLUDED.forwarded_at
""")


def _gone_table(session: Session) -> None:
    session.execute(text(f"CREATE TEMP TABLE IF NOT EXISTS {GONE_TABLE} "
                         "(id bigint, geom geometry, burn_before timestamptz) ON COMMIT DROP"))


def _delete_keeping_gone(session: Session, delete: str, params: dict[str, Any]) -> int:
    """Run `delete` (a DELETE FROM burn_cluster with the alias t) and keep the scars it deleted in
    GONE_TABLE until forward_gone. Returns the rows deleted."""
    return session.execute(text(f"WITH gone AS ({delete} RETURNING {GONE_COLS}) "
                                f"INSERT INTO {GONE_TABLE} SELECT * FROM gone"), params).rowcount


def forward_gone(session: Session) -> int:
    """Point the ids of the scars deleted so far in this transaction (GONE_TABLE) at their
    successors in burn_cluster_forward, then forget them. Returns the rows written.

    A case link (/case/<id>) must survive a rebuild that changes a scar's outline, a stage change
    that maps its district at another resolution and a rule change that moves the burn to another
    pass (each gives the scar a new id). A scar with no successor of the same burn gets no row (its
    link answers 404, as before). Best effort: a failure
    here is logged and rolled back to a savepoint, so it never costs the tile its rows."""
    try:
        with session.begin_nested():
            written = session.execute(_FORWARD_SQL).rowcount
    except DBAPIError as exc:
        log.warning("sentinel: scar ids not forwarded: %s", str(exc)[:300])
        written = 0
    session.execute(text(f"DELETE FROM {GONE_TABLE}"))
    return written


def claim_moved_rows(session: Session, tile: str, scope: str, area_wkt: str) -> dict[str, int]:
    """Delete the rows other stages stored on this tile that lie wholly inside this stage's area
    there (`area_wkt`, lon/lat): a district moved to this stage. Run right after this stage stored
    the tile, in the same transaction, so the moved area never holds both stages' rows, even when
    the old stage's turn on the tile never comes (a run that ran out of budget); each scar's id is
    forwarded to this stage's scar of the same burn. A row across the edge of the area is left to
    its own stage, whose next rebuild of the tile replaces it with its part outside the area, so no
    row is deleted twice. Returns rows deleted per table, and 'forwarded'."""
    if not area_wkt:
        return {}
    params = {"tile": tile, "scope": scope, "area": area_wkt}
    area = "(SELECT ST_GeomFromText(:area, 4326) AS g) a"
    _gone_table(session)
    claimed = {
        "burn_cluster": _delete_keeping_gone(session, f"""
            DELETE FROM burn_cluster t USING {area}
             WHERE t.mgrs_tile = :tile AND t.scope <> :scope AND ST_Covers(a.g, t.geom)""", params),
        "harvest_cluster": session.execute(text(f"""
            DELETE FROM harvest_cluster t USING {area}
             WHERE t.mgrs_tile = :tile AND t.scope <> :scope AND ST_Covers(a.g, t.geom)"""), params).rowcount,
    }
    claimed["forwarded"] = forward_gone(session) if claimed["burn_cluster"] else 0
    return claimed


def label_places(session: Session) -> dict[str, int]:
    """Names for rows that are still unnamed: new rows arrive named (_replace_rows), so this catches
    rows stored before the boundary and place tables were loaded."""
    out: dict[str, int] = {}
    for name in ("burn_cluster", "harvest_cluster"):
        out.update({f"{name}.{k}": n for k, n in _name_places(session, name).items()})
    return out


# --- the job ---------------------------------------------------------------------------------------


def drop_stage_rows(scope: str, tiles: list[str], dead: dict[str, DeadRows]) -> dict[str, int]:
    """Delete the rows a stage stored on tiles it no longer covers (a district moved to another
    stage, or a boundary change). A stage only ever replaces rows on tiles it processes, so these
    would otherwise stay for good, counted beside the rows the tile's new stage writes."""
    deleted = {}
    with session_scope() as session:
        _gone_table(session)
        params = {"scope": scope, "tiles": tiles}
        for name in ("burn_cluster", "harvest_cluster"):
            delete = f"DELETE FROM {name} t WHERE scope = :scope AND mgrs_tile = ANY(:tiles)"
            if name == "burn_cluster":  # their ids go to the scars the tile's new stage stored
                n = _delete_keeping_gone(session, delete, params)
            else:
                n = session.execute(text(delete), params).rowcount
            if n:
                deleted[name] = n
                dead[name].add(n)
        if deleted.get("burn_cluster"):
            deleted["forwarded"] = forward_gone(session)
    return deleted


def run_sentinel(*, budget_min: float | None = None, stages: tuple[str, ...] | None = None,
                 tiles: tuple[str, ...] | None = None) -> dict[str, Any]:
    """The whole run; keyword arguments narrow it (used by tests and manual backfills)."""
    started = time.perf_counter()
    budget_s = 60 * (budget_min if budget_min is not None else budget_minutes())
    now = utcnow()
    start = season_start(now)
    summary: dict[str, Any] = {"model_version": RULE.version, "rule": RULE.as_dict(),
                               "season_start": start.date().isoformat()}
    with session_scope() as session:
        areas, notes = stage_geometries(session)
    summary.update(notes)
    if not areas:
        summary["skipped"] = "boundary table has no Punjab/Haryana districts yet (boundaries module)"
        return summary
    union = shapely.union_all(list(areas.values()))
    scenes, summary["catalogue"] = refresh_catalogue(union, start, now)
    usable = [s for s in scenes if (s.cloud_pct or 0) <= MAX_CLOUD_PCT and (s.nodata_pct or 0) < MAX_NODATA_PCT]
    by_tile: dict[str, list[stac.SceneItem]] = defaultdict(list)
    for s in usable:
        by_tile[s.mgrs_tile].append(s)

    done: list[dict[str, Any]] = []
    errors: list[str] = []
    pending: list[str] = []
    # Rows each table's writes leave dead; vacuumed as they pile up, so the next tile, the fire
    # matching and then the risk job reuse that room instead of growing the files.
    dead = {name: DeadRows(name) for name in ("burn_cluster", "harvest_cluster")}
    for stage in STAGES:
        if stages and stage.name not in stages:
            continue
        area = areas.get(stage.name)
        if area is None or area.is_empty:
            continue
        # Tiles whose own square overlaps this stage's area, largest overlap first. A tile that
        # reaches it by less than MIN_STAGE_OVERLAP_M2 is left out, and whatever this stage stored
        # on it before is removed (drop_stage_rows): no later run of the stage would replace it.
        ranked, uncovered = [], []
        for tile, tile_scenes in by_tile.items():
            if tiles and tile not in tiles:
                continue
            grid = stac.TileGrid.from_scene(tile_scenes[0], stage.res)
            overlap = _from_lonlat(area, grid.epsg).intersection(shapely.box(*grid.core)).area
            if overlap >= MIN_STAGE_OVERLAP_M2:
                ranked.append((overlap, tile))
            else:
                uncovered.append(tile)
        if uncovered and (dropped := drop_stage_rows(stage.name, uncovered, dead)):
            summary.setdefault("dropped_stage_rows", {})[stage.name] = dropped
        for _overlap, tile in sorted(ranked, reverse=True):
            if time.perf_counter() - started > budget_s:
                pending.append(f"{stage.name}:{tile}")
                continue
            try:
                url = f"{stac.E84_STAC}/collections/{stac.E84_COLLECTION}/items?grid:code=MGRS-{tile}"
                with track_fetch("s2_cog", url=url, meta={"tile": tile, "stage": stage.name,
                                                          "res_m": stage.res}) as tracker:
                    res = process_tile(stage, area, tile, by_tile[tile], now)
                    tracker.records = res.windows_read if res is not None else 0
                    if res is not None:
                        tracker.meta.update(passes=res.passes, blocks=res.blocks)
                if res is None:
                    continue
                stored = store_tile(res, tracker.fetch_id)
                for name, counts in stored.items():
                    # deleting and updating both leave a dead row version
                    dead[name].add(counts["deleted"] + counts["updated"] + counts["claimed"])
                done.append({
                    "stage": stage.name, "tile": tile, "passes": res.passes, "blocks": res.blocks,
                    "burn_clusters": len(res.burn_rows), "filtered_lookalikes": res.filtered_clusters,
                    "harvest_clusters": len(res.harvest_rows),
                    "hazy_passes": sorted(res.hazy_passes), "gate_failures": res.gate_failures[:5],
                    "rebuilt": res.rebuilt, "seconds": res.seconds, "stored": stored,
                })
                log.info("sentinel %s %s: %s", stage.name, tile, done[-1])
            except Exception as exc:  # one bad tile must not stop the others
                log.exception("sentinel: tile %s (%s) failed", tile, stage.name)
                errors.append(f"{stage.name}:{tile}: {type(exc).__name__}: {exc}"[:300])
    summary.update(tiles=done, errors=errors, pending=pending)
    with session_scope() as session:
        summary["cluster_filter"] = apply_cluster_filter(session)
    summary["fire_match"] = match_fires_in_batches(dead=dead["burn_cluster"])
    with session_scope() as session:
        summary["labels"] = label_places(session)
    # Whatever is still dead goes before the risk job writes a score onto every new harvested field.
    for counter in dead.values():
        counter.finish()
    summary["vacuums"] = {name: counter.vacuums for name, counter in dead.items()}
    with session_scope() as session:
        summary["totals"] = dict(session.execute(text(
            "SELECT count(*) AS burn_clusters, coalesce(round(sum(area_ha)::numeric, 1), 0) AS burn_area_ha, "
            "count(*) FILTER (WHERE unseen) AS unseen_clusters FROM burn_cluster WHERE model_version = :v"
        ), {"v": RULE.version}).mappings().one())
        summary["totals"]["harvest_clusters"] = session.execute(
            select(func.count()).select_from(HarvestCluster)).scalar()
    summary["seconds"] = round(time.perf_counter() - started, 1)
    if errors and not done:
        raise RuntimeError(f"every tile failed: {errors[:3]}")
    return summary


@job("sentinel", every_minutes=360)
def sentinel() -> dict[str, Any]:
    """New clear Sentinel-2 passes -> burn scars, harvested-unburnt fields, fire matching."""
    return run_sentinel()
