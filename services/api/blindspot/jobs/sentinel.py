"""Job `sentinel` (every 6 h): new clear Sentinel-2 passes -> burn scars, harvested fields, fire match.

What one run does:
1. Catalogue: search Earth Search for every S2 L2A item over Punjab and Haryana since the season
   started, and record each in s2_scene (too cloudy or almost all nodata -> `skipped`).
2. For each processing stage in priority order (hotspot districts at 20 m, then the rest of Punjab,
   then Haryana at 40 m), for each MGRS tile: read only the passes this tile has not folded in yet,
   block by block (1024 x 1024 px, aligned with the COG tiles, about 150 MB of RAM per block), and
   let each pixel compare itself with its own previous clear pass (analytics.burnscar). Pixels are
   judged only inside the stage's districts, on cropland, and in the tile's own 100 km MGRS square,
   so the ~10 km overlap between tiles is never counted twice.
3. Stitch clusters cut by block edges, drop those under 0.52 ha, and replace this tile's rows for
   the passes just processed (so a rerun after a crash never duplicates a scar). The harvested-but-
   unburnt snapshot of the tile is rebuilt the same way.
4. Match every scar against fire detections (fire_detection, filled by the fires module) and name
   its district, sub-district and nearest village from the boundary and place tables.

A run stops starting new tiles after SENTINEL_BUDGET_MIN minutes (default 50); the next run picks
up where it stopped, because each block remembers which passes it has folded in (an .npz file per
block under CACHE_DIR/sentinel). A tile's memory is committed only after its rows are in the
database, so a crash just redoes that tile.
"""

from __future__ import annotations

import datetime as dt
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
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from blindspot.analytics import burnscar
from blindspot.analytics.burnscar import RULE, BlockState, Part
from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.geo import fill_admin
from blindspot.jobs import job
from blindspot.models.sentinel import BurnCluster, HarvestCluster, S2Scene
from blindspot.provenance import track_fetch, utcnow
from blindspot.sources import stac

log = logging.getLogger(__name__)

BLOCK_PX = 1024  # 2 x 2 internal COG tiles of the 20 m bands
MAX_CLOUD_PCT = 70.0  # skip passes whose whole tile is mostly cloud (doc section 4, step 1)
MAX_NODATA_PCT = 99.0  # a swath-edge sliver with nothing to read
SETTLE_HOURS = 6  # wait this long after sensing, so every granule of the pass is in the catalogue
SEASON_MONTH, SEASON_DAY = 10, 1  # the paddy-residue season we monitor starts on 1 October
VILLAGE_MAX_KM = 3.0  # name the nearest village only if it is this close
MATCH_TOLERANCE_H = 3  # a detection up to 3 h after the post pass still counts as "seen" (smouldering)
PASS_READERS = 3  # passes of one block read at once (each reads its 10 bands with 12 threads)

# The districts that lead the official burning counts year after year; they are processed first
# and at full 20 m resolution. Names are matched case-insensitively against the boundary table,
# with the spellings different datasets use (LGD "Ferozepur", geoBoundaries "Firozpur").
HOTSPOT_DISTRICTS: dict[str, tuple[str, ...]] = {
    "Sangrur": ("sangrur",),
    "Bathinda": ("bathinda", "bhatinda"),
    "Mansa": ("mansa",),
    "Barnala": ("barnala",),
    "Patiala": ("patiala",),
    "Moga": ("moga",),
    "Firozpur": ("firozpur", "ferozepur", "firozepur", "ferozpur"),
    "Ludhiana": ("ludhiana",),
    "Fatehgarh Sahib": ("fatehgarh sahib",),
    "Tarn Taran": ("tarn taran",),
    "Amritsar": ("amritsar",),
}


@dataclass(frozen=True)
class Stage:
    """One processing scope: a name, the pixel size used there, and how to find its polygon."""

    name: str
    res: int
    doc: str


STAGES = (
    Stage("hotspots", 20, "11 hotspot districts of Punjab, 20 m (field scale: a 1 ha plot is ~25 px)"),
    Stage("punjab", 40, "rest of Punjab, 40 m COG overviews (state-scale area; ~6 px per 1 ha plot)"),
    Stage("haryana", 40, "Haryana, 40 m COG overviews"),
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

    Returns ({stage: geometry}, notes). Empty when the boundaries have not been loaded yet."""
    notes: dict[str, Any] = {}
    names = sorted({alias for aliases in HOTSPOT_DISTRICTS.values() for alias in aliases})
    hotspots = _union(
        session,
        "level = 'district' AND lower(state) = 'punjab' AND lower(name) = ANY(:names)",
        {"names": names},
    )
    found = session.execute(
        text("SELECT name FROM boundary WHERE level = 'district' AND lower(state) = 'punjab' "
             "AND lower(name) = ANY(:names) ORDER BY name"),
        {"names": names},
    ).scalars().all()
    notes["hotspot_districts_found"] = list(found)
    out: dict[str, BaseGeometry] = {}
    punjab = _union(session, "level = 'state' AND lower(name) = 'punjab'", {}) or _union(
        session, "level = 'district' AND lower(state) = 'punjab'", {}
    )
    haryana = _union(session, "level = 'state' AND lower(name) = 'haryana'", {}) or _union(
        session, "level = 'district' AND lower(state) = 'haryana'", {}
    )
    if hotspots is not None and not hotspots.is_empty:
        out["hotspots"] = hotspots.buffer(0)
    if punjab is not None and not punjab.is_empty:
        rest = punjab.buffer(0)
        if "hotspots" in out:
            rest = rest.difference(out["hotspots"])
        out["punjab"] = rest
    if haryana is not None and not haryana.is_empty:
        out["haryana"] = haryana.buffer(0)
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


def _tile_dir(stage: Stage, tile: str) -> Path:
    return state_root() / RULE.pixel_version / stage.name / f"{tile}_{stage.res}m"


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
    return _finish_tile(stage, tile, grid, scenes, passes, pass_days, registry, sensing, next_dir,
                        burn_parts, harvest_parts, result, t0)


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


def store_tile(result: TileResult, fetch_id: int | None) -> None:
    """Replace this tile's rows for the passes just processed, then swap in its new memory."""
    stage_name, tile = result.stage, result.tile
    with session_scope() as session:
        cond = (BurnCluster.mgrs_tile == tile) & (BurnCluster.scope == stage_name)
        if result.rebuilt:
            session.execute(delete(BurnCluster).where(cond))
        elif result.post_days:
            # Only rows of blocks that were reprocessed for those passes; others stay as they are.
            days = [stac.from_day_number(d) for d in result.post_days]
            session.execute(delete(BurnCluster).where(
                cond & func.date(BurnCluster.burn_before).in_(days)
                & BurnCluster.block.in_(sorted(result.processed_blocks))))
        if result.burn_rows:
            session.execute(insert(BurnCluster), [{**r, "fetch_id": fetch_id} for r in result.burn_rows])
        session.execute(delete(HarvestCluster).where(
            (HarvestCluster.mgrs_tile == tile) & (HarvestCluster.scope == stage_name)))
        if result.harvest_rows:
            session.execute(insert(HarvestCluster), [{**r, "fetch_id": fetch_id} for r in result.harvest_rows])
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
        WHERE {_COVERED}
        GROUP BY b.id
    )
    UPDATE burn_cluster b
       SET fire_matches = m.n, unseen = (m.n = 0), nearest_fire_m = round(m.nearest::numeric, 1),
           matched_at = now()
      FROM m
     WHERE m.id = b.id
    """
)
_UNCOVERED_SQL = text(
    f"""
    UPDATE burn_cluster b SET fire_matches = NULL, unseen = NULL, nearest_fire_m = NULL, matched_at = now()
     WHERE NOT ({_COVERED}) AND (b.unseen IS NOT NULL OR b.matched_at IS NULL)
    """
)


def covered_days(session: Session) -> tuple[list, str]:
    """UTC days for which we hold fire records from every FIRMS source.

    Uses the fires module's fire_coverage table (one row per source and day fetched from the FIRMS
    area API). Without it, falls back to every day between the first and last detection stored."""
    if session.execute(text("SELECT to_regclass('public.fire_coverage')")).scalar() is not None:
        days = session.execute(text(
            "SELECT day FROM fire_coverage GROUP BY day "
            "HAVING count(DISTINCT source) = (SELECT count(DISTINCT source) FROM fire_coverage) ORDER BY day"
        )).scalars().all()
        if days:
            return list(days), "fire_coverage: days fetched for every FIRMS source"
    first, last = session.execute(text("SELECT min(acq_utc)::date, max(acq_utc)::date FROM fire_detection")).one()
    if first is None:
        return [], "no fire records"
    n = (last - first).days + 1
    return [first + dt.timedelta(days=i) for i in range(n)], "first..last stored detection (no fire_coverage)"


def match_fires(session: Session) -> dict[str, Any]:
    """Count fire detections that explain each scar and set `unseen`.

    A detection explains a scar when it lies within max(1 km, its own pixel half-diagonal + 200 m)
    of the scar polygon (SYNTHESIS section 2.5: VIIRS 465-760 m, MODIS ~1-2.8 km; never less than
    1 km, so "unseen" stays conservative) and was acquired between the clear pass before the burn
    and 3 h after the pass that shows it. A scar gets a verdict only when fire records cover every
    day of its window for every FIRMS source; otherwise unseen stays NULL: no data is not the same as
    no fire. Re-run every job run, so verdicts follow late-arriving fire data."""
    if session.execute(text("SELECT to_regclass('public.fire_detection')")).scalar() is None:
        return {"skipped": "fire_detection table does not exist yet (fires module)"}
    n_fires = session.execute(text("SELECT count(*) FROM fire_detection")).scalar()
    if not n_fires:
        return {"skipped": "fire_detection is empty"}
    days, basis = covered_days(session)
    matched = session.execute(_MATCH_SQL, {"days": days}).rowcount
    unknown = session.execute(_UNCOVERED_SQL, {"days": days}).rowcount
    return {"fire_records": n_fires, "covered_days": [str(days[0]), str(days[-1]), len(days)] if days else [],
            "coverage_basis": basis, "clusters_matched": matched, "clusters_without_fire_coverage": unknown}


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


def label_places(session: Session) -> dict[str, int]:
    """District/state (via geo.fill_admin), sub-district and nearest village for unnamed rows."""
    out: dict[str, int] = {}
    for table in ("burn_cluster", "harvest_cluster"):
        out[f"{table}.district"] = fill_admin(session, table)
        out[f"{table}.subdistrict"] = session.execute(text(_SUBDISTRICT_SQL.format(table=table))).rowcount
        out[f"{table}.village"] = session.execute(
            text(_VILLAGE_SQL.format(table=table)), {"max_km": VILLAGE_MAX_KM}
        ).rowcount
    return out


# --- the job ---------------------------------------------------------------------------------------


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
    for stage in STAGES:
        if stages and stage.name not in stages:
            continue
        area = areas.get(stage.name)
        if area is None or area.is_empty:
            continue
        # Tiles whose own square overlaps this stage's area, largest overlap first.
        ranked = []
        for tile, tile_scenes in by_tile.items():
            if tiles and tile not in tiles:
                continue
            grid = stac.TileGrid.from_scene(tile_scenes[0], stage.res)
            overlap = _from_lonlat(area, grid.epsg).intersection(shapely.box(*grid.core)).area
            if overlap > 0:
                ranked.append((overlap, tile))
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
                store_tile(res, tracker.fetch_id)
                done.append({
                    "stage": stage.name, "tile": tile, "passes": res.passes, "blocks": res.blocks,
                    "burn_clusters": len(res.burn_rows), "filtered_lookalikes": res.filtered_clusters,
                    "harvest_clusters": len(res.harvest_rows),
                    "hazy_passes": sorted(res.hazy_passes), "gate_failures": res.gate_failures[:5],
                    "rebuilt": res.rebuilt, "seconds": res.seconds,
                })
                log.info("sentinel %s %s: %s", stage.name, tile, done[-1])
            except Exception as exc:  # one bad tile must not stop the others
                log.exception("sentinel: tile %s (%s) failed", tile, stage.name)
                errors.append(f"{stage.name}:{tile}: {type(exc).__name__}: {exc}"[:300])
    summary.update(tiles=done, errors=errors, pending=pending)
    with session_scope() as session:
        summary["cluster_filter"] = apply_cluster_filter(session)
    with session_scope() as session:
        summary["fire_match"] = match_fires(session)
    with session_scope() as session:
        summary["labels"] = label_places(session)
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
