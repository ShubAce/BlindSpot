"""Evaluate the haze-corrected burn rule (s2burn-rules-4h) against s2burn-rules-2 on six windows.

The candidate (analytics.burnscar.normalise_haze): on a pass that haze_check calls hazy, every band
is brought to the radiometry of the block's last clear pass by subtracting a per-band offset,
measured cell by cell (~5 km) as the median change of unchanged ground (SWIR-2, SWIR-1 and NIR
stable), clipped at 0 and made non-increasing with wavelength, before any burn test; the corrected
values are what the memory keeps. Everything else is rules-2.

What `replay` does per window, as the sentinel job does it (jobs/sentinel.py): it finds every
Earth Search item of the window's tile from 1 October to the 22 November pass, reads each pass over
every production block (1024 x 1024 px at the stage's resolution) that touches the window (plus
600 m), judges only the stage's area (the stage polygon from the boundary table) on WorldCover
cropland in the tile's own 100 km square, and folds the passes in date order into two block
memories, one per rule, with analytics.burnscar.apply_pass on the same reads. Clusters are built
with the job's code: extract_parts per block, merge_parts across blocks, the minimum size
(RULE.min_px), _clean_polygon and the look-alike filter (keeps). Areas are counted on the window
(inside the stage area and the tile's square) from the season masks: pixels of kept clusters from
1 October through the 22 November pass. Reads are cached (DN), so reruns download nothing; nothing
is written to any database (provenance rows off, the eval database opened read-only).

Windows (production resolution of each stage):
  W1 case 4723594 Agwar Gujjran, Ludhiana, 43REQ, hotspots 20 m: the case box enlarged to 3 x 3 km
  W2 case 4936479 Ratta Theh, 43REN, haryana 40 m: the case box
  W3 case 4939467 Kalwan, 43REN, haryana 40 m: the case box
  W4 Fazilka, 43RDP, hotspots 20 m: lon/lat 74.086948, 30.512422, 74.153693, 30.548355
  W5 Barnala, 43REP, hotspots 20 m: UTM 43N 545000, 3362000, 555000, 3372000
  W6 Tarn Taran, 43REQ, hotspots 20 m: UTM 43N 503000, 3480500, 513000, 3490500

PRE-REGISTERED BAR (written 2026-11-24, before the first evaluation run; committed with this file)
Ship s2burn-rules-4h only if all four hold:
 (a) AREA: added area (candidate-only season pixels in the window) >= 10% of the baseline
     (rules-2) area in at least 3 of the 6 windows.
 (b) BLIND AUDIT: precision of the 'added' stratum >= max(0.6, precision of the 'baseline'
     stratum - 0.10); a chip counts as burnt only on a 'burnt' verdict ('uncertain' = not burnt).
 (c) VALID AUDIT: judges call >= 70% of the positive controls burnt and >= 70% of the negative
     controls not burnt (decided chips).
 (d) RUNTIME: the daily sentinel run stays within SENTINEL_BUDGET_MIN = 100 min: today's 54.6 min
     (job_run 2065, 23 Nov) plus the extra compute per block-pass measured here (candidate minus
     baseline apply_pass time, mean over every block-pass replayed, at each resolution) times
     production's block-passes (4188 at most in run 2065), plus the extra block-memory save time
     per block times its 227 blocks, must be <= 100 min, also with 25% more passes.
The orchestrator's verifier applies (b) and (c) from blind judges; this script reports (a) and (d).
A candidate that misses any part is not shipped.

Audit chips (`chips`): 30 'added' (4-connected pieces of the candidate-only season area of at
least the rule's minimum cluster size, outlined as their own clusters), 20 'baseline' (kept
rules-2 clusters), 10 'positive_control' (stored production scars with a fire alert inside them in
their window: fire_matches > 0 or unseen = false, eval database), 10 'negative_control' (cropland
patches shaped like a random baseline cluster, on its pass pair, that did not darken and lie in no
scar), all drawn with random.Random(20261124) from the windows' anchors. Each chip shows the same
~1.2 km square at 10 m four times: true colour (B04, B03, B02) on the cluster's own pre pass and
on its own post pass, SWIR false colour (B12, B8A, B04) on each, in the site's colour recipes
(sources/pcdata.colourise), the audited area outlined in yellow, the two dates in a caption, and
nothing else. Files are chip_001.png ... in shuffled order; the key goes to a separate JSON.

Run from services/api:
  uv run python scripts/burn_haze_eval.py replay <out> [W1 ...] [--db URL] [--cache DIR]
  uv run python scripts/burn_haze_eval.py chips <out> <chips_dir> <key_json>
  uv run python scripts/burn_haze_eval.py summary <out>
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import io
import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from rasterio import features
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import box, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api: the blindspot package
from blindspot import provenance

provenance.record_fetch = lambda *a, **k: None  # an evaluation leaves no source_fetch rows anywhere

from blindspot.analytics import burnscar
from blindspot.analytics.burnscar import RULE, BlockState
from blindspot.jobs import sentinel as job
from blindspot.sources import stac

CAND = RULE
BASE = dataclasses.replace(RULE, haze_norm=False, pixel_version="s2burn-rules-1", version="s2burn-rules-2")
RULES = {"base": BASE, "cand": CAND}
DEFAULT_DB = "postgresql+psycopg://blindspot:blindspot@localhost:5434/blindspot_eval"
SCRATCH = Path(r"C:/Users/shubh/AppData/Local/Temp/claude/4e86b8c1d039525b/f34d730e-7696-47d0-946a-e4f2c89a45cf/scratchpad")
SHARED_CACHES = [SCRATCH / "burnscars" / "cache"]  # read-only: 20 m production blocks, <tile>/r_c/<day>.npz
RES_CACHE = SCRATCH / "res" / "cache"  # read-only: 20 m windows of the 43REN cases (burn_res_replay.py)
START, END = "2026-10-01", "2026-11-22T23:59:59"
PRE, POST = dt.date(2026, 11, 17), dt.date(2026, 11, 22)
VISIBLE = {"bais2_min": 0.70, "tbi_max": 0.40, "d_b12": 0.02, "d_b8a": 0.04}  # as burn_res_replay.VISIBLE
SEED = 20261124
PROD = {"run": 2065, "minutes": 54.6, "block_passes": 4188, "blocks": 227, "budget": 100.0}


@dataclass(frozen=True)
class Win:
    name: str
    tile: str
    stage: str
    res: int
    bounds: tuple[float, float, float, float]
    lonlat: bool
    case_id: int | None = None
    enlarge_m: float = 0.0  # >0: a square of this side centred on the box


WINDOWS = [
    Win("W1", "43REQ", "hotspots", 20, (75.44845, 30.820457, 75.461004, 30.831237), True, 4723594, 3000.0),
    Win("W2", "43REN", "haryana", 40, (75.70084, 29.681656, 75.741794, 29.71723), True, 4936479),
    Win("W3", "43REN", "haryana", 40, (75.970462, 29.698501, 75.998314, 29.722692), True, 4939467),
    Win("W4", "43RDP", "hotspots", 20, (74.086948, 30.512422, 74.153693, 30.548355), True),
    Win("W5", "43REP", "hotspots", 20, (545000, 3362000, 555000, 3372000), False),
    Win("W6", "43REQ", "hotspots", 20, (503000, 3480500, 513000, 3490500), False),
]


def db_session(db_url: str) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    return Session(create_engine(db_url, connect_args={"options": "-c default_transaction_read_only=on"}))


def search(tile: str, bbox_ll: tuple[float, ...]) -> list[stac.SceneItem]:
    start = dt.datetime.fromisoformat(START).replace(tzinfo=dt.UTC)
    end = dt.datetime.fromisoformat(END).replace(tzinfo=dt.UTC)
    scenes, _ = stac.search_scenes(mapping(box(*bbox_ll)), start, end)
    return [s for s in scenes if s.mgrs_tile == tile and (s.cloud_pct or 0) <= job.MAX_CLOUD_PCT
            and (s.nodata_pct or 0) < job.MAX_NODATA_PCT]


def inner_utm(w: Win, grid: stac.TileGrid) -> tuple[float, float, float, float]:
    """The window in the tile's UTM metres, snapped outward to the stage's pixel grid."""
    g = job._from_lonlat(box(*w.bounds), grid.epsg).envelope if w.lonlat else box(*w.bounds)
    if w.enlarge_m:
        c = g.centroid
        g = box(c.x - w.enlarge_m / 2, c.y - w.enlarge_m / 2, c.x + w.enlarge_m / 2, c.y + w.enlarge_m / 2)
    x0, y0, x1, y1 = g.bounds
    r = grid.res
    return (grid.x0 + math.floor((x0 - grid.x0) / r) * r, grid.y0 - math.ceil((grid.y0 - y0) / r) * r,
            grid.x0 + math.ceil((x1 - grid.x0) / r) * r, grid.y0 - math.floor((grid.y0 - y1) / r) * r)


def cache_path(cache: Path, tile: str, res: int, w: Window, day: int | str) -> Path:
    return cache / tile / f"{res}m" / f"r{w.row_off}_c{w.col_off}" / f"{day}"


def read_block(cache: Path, tile: str, res: int, grid: stac.TileGrid, w: Window, day: int,
               items: list[stac.SceneItem]) -> tuple[stac.BlockRead, float]:
    """One pass over one production block (cached DN): (read, seconds spent downloading)."""
    shared = [c / tile / f"r{w.row_off}_c{w.col_off}" / f"{day}.npz" for c in SHARED_CACHES] if res == 20 else []
    path = cache_path(cache, tile, res, w, day).with_suffix(".npz")
    for p in [*shared, path]:
        if p.exists():
            with np.load(p) as d:
                return stac.BlockRead({b: d[b] for b in (*stac.REFLECTANCE_BANDS, "SCL")}, d["item_index"],
                                      json.loads(bytes(d["items"]).decode()), 0), 0.0
    t0 = time.perf_counter()
    read = stac.read_pass_block(items, grid.window_bounds(w), (w.height, w.width))
    seconds = time.perf_counter() - t0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, item_index=read.item_index, items=np.frombuffer(json.dumps(read.items).encode(), np.uint8),
                        **read.dn)
    tmp.replace(path)
    return read, seconds


def cropland(cache: Path, tile: str, res: int, grid: stac.TileGrid, w: Window) -> np.ndarray:
    shared = [c / tile / f"r{w.row_off}_c{w.col_off}" / "cropland.npy" for c in SHARED_CACHES] if res == 20 else []
    path = cache_path(cache, tile, res, w, "cropland").with_suffix(".npy")
    for p in [*shared, path]:
        if p.exists():
            return np.load(p)
    hrefs, _ = stac.worldcover_hrefs(stac.utm_bounds_to_lonlat(grid.epsg, grid.window_bounds(w)))
    mask = stac.read_worldcover(hrefs, grid.epsg, grid.window_transform(w), (w.height, w.width)) == stac.WORLDCOVER_CROPLAND
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, mask)
    return mask


def rasterize(geoms: list[Any], shape_: tuple[int, int], transform: Any) -> np.ndarray:
    if not geoms:
        return np.zeros(shape_, bool)
    return features.rasterize([(g, 1) for g in geoms], out_shape=shape_, transform=transform, dtype=np.uint8).astype(bool)


def to_refl(read: stac.BlockRead) -> dict[str, np.ndarray]:
    # Earth Search c1, processing baseline 05.x: scale 1e-4, offset -0.1 for every band (stac.reflectance).
    return {b: stac.reflectance(read.dn[b], 1e-4, -0.1) for b in stac.REFLECTANCE_BANDS}


def kept_clusters(parts: dict[int, list[burnscar.Part]], registry: list[str], res: int,
                  sensing: dict[str, dt.datetime]) -> list[dict[str, Any]]:
    """The job's _finish_tile for one rule, minus the database."""
    out = []
    min_px = RULE.min_px(res)
    for post_day, ps in sorted(parts.items()):
        for part in burnscar.iter_large(burnscar.merge_parts(ps), min_px):
            poly = job._clean_polygon(part.geom, res)
            if poly is None or not RULE.keeps(part.mean("nbr_post")):
                continue
            pre_id, post_id = registry[part.mode("pre_item") or 0], registry[part.mode("post_item") or 0]
            out.append({"post_day": post_day, "pre": str(sensing[pre_id].date()) if pre_id in sensing else None,
                        "post": str(stac.from_day_number(post_day)), "pre_item": pre_id, "post_item": post_id,
                        "n_px": part.n_px, "area_ha": round(part.n_px * res * res / 10_000, 3),
                        "hazy": bool(part.mode("hazy")), "geom_px": part.geom, "geom": poly,
                        "means": {k: round(part.mean(k) or 0.0, 4) for k in ("d_b8a", "d_b12", "tbi_post", "bais2_post")}})
    return out


def replay_window(w: Win, out: Path, cache: Path, areas: dict[str, Any]) -> dict[str, Any]:
    t_start = time.perf_counter()
    probe = w.bounds if w.lonlat else None
    if probe is None:
        probe = stac.utm_bounds_to_lonlat(32643, w.bounds)
    first = search(w.tile, tuple(box(*probe).buffer(0.01).bounds))
    grid = stac.TileGrid.from_scene(first[0], w.res)
    inner = inner_utm(w, grid)
    area_utm = job._from_lonlat(areas[w.stage], grid.epsg).intersection(box(*grid.core))
    reach = box(*inner).buffer(600)
    blocks = [b for b in grid.blocks(job.BLOCK_PX) if reach.intersects(box(*grid.window_bounds(b)))
              and area_utm.intersects(box(*grid.window_bounds(b)))]
    union_ll = stac.utm_bounds_to_lonlat(grid.epsg, shapely.union_all([box(*grid.window_bounds(b)) for b in blocks]).bounds)
    usable = search(w.tile, union_ll)
    passes: dict[int, list[stac.SceneItem]] = defaultdict(list)
    for s in usable:
        passes[stac.day_number(s.datetime)].append(s)
    days = sorted(passes)
    sensing = {s.item_id: s.datetime for s in usable}
    footprints = {s.item_id: job._from_lonlat(shape(s.geometry), grid.epsg) for s in usable}
    print(f"{w.name} {w.tile} {w.stage} {w.res} m: inner {inner}, blocks {[job._block_id(b) for b in blocks]}, "
          f"{len(days)} passes", flush=True)
    registry: dict[str, list[str]] = {k: [] for k in RULES}
    parts: dict[str, dict[int, list[burnscar.Part]]] = {k: defaultdict(list) for k in RULES}
    timing = {"read_s": 0.0, "base_s": 0.0, "cand_s": 0.0, "block_passes": 0, "save_base_s": 0.0, "save_cand_s": 0.0}
    norm_log: list[dict[str, Any]] = []
    diag: dict[str, dict[int, dict[str, np.ndarray]]] = {}
    pre_d, post_d = stac.day_number(PRE), stac.day_number(POST)
    for b in blocks:
        bid = job._block_id(b)
        btf = grid.window_transform(b)
        bshape = (b.height, b.width)
        scope = rasterize([area_utm], bshape, btf) & grid.core_mask(b)
        if scope.any():
            scope &= cropland(cache, w.tile, w.res, grid, b)
        states = {k: BlockState.empty(bshape) for k in RULES}
        bbox = box(*grid.window_bounds(b))
        for day in days:
            items = [s for s in passes[day] if footprints[s.item_id].intersects(bbox)]
            if not items or not scope.any():
                continue
            read, secs = read_block(cache, w.tile, w.res, grid, b, day, items)
            timing["read_s"] += secs
            refl = to_refl(read)
            outs = {}
            for k, rule in RULES.items():
                t0 = time.perf_counter()
                outs[k] = burnscar.apply_pass(states[k], refl, read.dn["SCL"], read.items, read.item_index, day,
                                              scope, registry[k], rule=rule, res_m=w.res)
                timing[f"{k}_s"] += time.perf_counter() - t0
            timing["block_passes"] += 1
            c = outs["cand"]
            if c.hazy:
                norm_log.append({"block": bid, "day": str(stac.from_day_number(day)),
                                 "haze_b02_rise": c.stats.get("haze_b02_rise"), **c.stats.get("haze_norm", {})})
            for k, o in outs.items():
                if o.used and o.burn is not None and o.burn.any():
                    ev = {kk: np.where(o.burn, v, 0).astype(np.float32) for kk, v in o.evidence.items()}
                    tags = {"pre_item": o.pre_item, "post_item": o.post_item,
                            "hazy": np.full(o.burn.shape, int(o.hazy), np.uint16)}
                    parts[k][day].extend(burnscar.extract_parts(o.burn, o.pre_day, btf, ev, tags))
            if day in (pre_d, post_d):  # raw pass for the "visibly charred" check (W1, at 20 m)
                valid = scope & burnscar.valid_scl(read.dn["SCL"]) & np.isfinite(refl["B12"]) & np.isfinite(refl["B8A"])
                diag.setdefault(bid, {})[day] = {**{x: refl[x] for x in ("B8A", "B12", "B02", "B03", "B04", "B06",
                                                                         "B07", "B08", "B11")}, "valid": valid}
            del read, refl, outs
        for k in RULES:  # block-memory save time (production writes every block's memory once a run)
            t0 = time.perf_counter()
            buf = io.BytesIO()
            np.savez_compressed(buf, **states[k].arrays)
            timing[f"save_{k}_s"] += time.perf_counter() - t0
        print(f"  block {bid} done ({time.perf_counter() - t_start:.0f} s)", flush=True)

    kept = {k: kept_clusters(parts[k], registry[k], w.res, sensing) for k in RULES}
    # Window grid at the stage resolution; region = inside the stage area and the tile's square.
    wshape = (round((inner[3] - inner[1]) / w.res), round((inner[2] - inner[0]) / w.res))
    wtf = from_origin(inner[0], inner[3], w.res, w.res)
    win_window = Window(round((inner[0] - grid.x0) / w.res), round((grid.y0 - inner[3]) / w.res), wshape[1], wshape[0])
    region = rasterize([area_utm], wshape, wtf) & grid.core_mask(win_window)
    season = {k: rasterize([c["geom_px"] for c in kept[k]], wshape, wtf) & region for k in RULES}
    px_ha = w.res * w.res / 10_000
    base_ha, cand_ha = float(season["base"].sum()) * px_ha, float(season["cand"].sum()) * px_ha
    added = season["cand"] & ~season["base"]
    removed = season["base"] & ~season["cand"]
    # Added pieces (4-connected) and their sizes: the audit's 'added' units.
    from scipy import ndimage

    lab, n = ndimage.label(added, structure=burnscar.FOUR_CONNECTED)
    sizes = ndimage.sum_labels(added, lab, np.arange(1, n + 1)).astype(int) if n else np.array([], int)
    min_px = RULE.min_px(w.res)
    inner_box = box(*inner)
    added_units = []
    tree = shapely.STRtree([c["geom_px"] for c in kept["cand"]])
    for i in np.flatnonzero(sizes >= min_px):
        m = lab == i + 1
        geoms = [shape(g) for g, v in features.shapes(m.astype(np.uint8), mask=m, transform=wtf, connectivity=4) if v]
        geom = max(geoms, key=lambda g: g.area)
        # Dates: the candidate cluster that covers most of the piece.
        near = [kept["cand"][int(j)] for j in tree.query(geom, predicate="intersects")]
        best = max(near, key=lambda c: c["geom_px"].intersection(geom).area)
        added_units.append({"geom": geom.wkt, "n_px": int(sizes[i]), "area_ha": round(int(sizes[i]) * px_ha, 3),
                            "pre": best["pre"], "post": best["post"], "pre_item": best["pre_item"],
                            "post_item": best["post_item"], "hazy": best["hazy"]})
    report: dict[str, Any] = {
        "window": w.name, "tile": w.tile, "stage": w.stage, "res_m": w.res, "case_id": w.case_id, "epsg": grid.epsg,
        "inner_utm": inner, "blocks": [job._block_id(b) for b in blocks],
        "passes": [str(stac.from_day_number(d)) for d in days],
        "region_ha": round(float(region.sum()) * px_ha, 1),
        "baseline_ha": round(base_ha, 2), "candidate_ha": round(cand_ha, 2),
        "added_ha": round(float(added.sum()) * px_ha, 2), "removed_ha": round(float(removed.sum()) * px_ha, 2),
        "added_share_of_baseline": round(float(added.sum()) * px_ha / base_ha, 3) if base_ha else None,
        "added_in_pieces_ge_min_size_ha": round(float(sizes[sizes >= min_px].sum()) * px_ha, 2),
        "added_pieces_ge_min_size": len(added_units),
        "kept_clusters": {k: len(kept[k]) for k in RULES},
        "kept_clusters_anchor_in_window": {k: sum(inner_box.contains(job._anchor(c["geom"])) for c in kept[k])
                                           for k in RULES},
        "hazy_block_passes": len(norm_log), "normalised_block_passes": sum(bool(x.get("applied")) for x in norm_log),
        "normalisation": norm_log,
        "timing": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in timing.items()},
    }
    if w.case_id is not None:
        report["visible_17_22"] = visible_check(w, grid, inner, wshape, wtf, region, kept, diag, cache)
    report["seconds"] = round(time.perf_counter() - t_start, 1)
    wdir = out / w.name
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "report.json").write_text(json.dumps(report, indent=1))
    clusters = [{"rule": k, **{kk: v for kk, v in c.items() if kk not in ("geom_px", "geom")}, "geom": c["geom"].wkt,
                 "anchor_in_window": bool(inner_box.contains(job._anchor(c["geom"])))} for k in RULES for c in kept[k]]
    (wdir / "clusters.json").write_text(json.dumps({"tile": w.tile, "epsg": grid.epsg, "res": w.res, "inner": inner,
                                                    "clusters": clusters, "added": added_units}))
    np.savez_compressed(wdir / "masks.npz", base=season["base"], cand=season["cand"], region=region)
    print(json.dumps({k: v for k, v in report.items() if k != "normalisation"}, indent=1), flush=True)
    return report


def stored_scars(db_url: str, tile: str, epsg: int, bounds: tuple[float, ...]) -> list[dict[str, Any]]:
    from sqlalchemy import text

    ll = stac.utm_bounds_to_lonlat(epsg, bounds)
    with db_session(db_url) as session:
        rows = session.execute(text("""
            SELECT id, burn_after, burn_before, scene_pre, scene_post, fire_matches, unseen, res_m, area_ha,
                   ST_AsText(geom) AS wkt FROM burn_cluster
            WHERE mgrs_tile = :tile AND geom && ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326)"""),
            {"tile": tile, "x0": ll[0], "y0": ll[1], "x1": ll[2], "y1": ll[3]}).mappings().all()
    return [dict(r) for r in rows]


def visible_check(w: Win, grid: stac.TileGrid, inner: tuple[float, ...], wshape: tuple[int, int], wtf: Any,
                  region: np.ndarray, kept: dict[str, list[dict[str, Any]]], diag: dict, cache: Path) -> dict[str, Any]:
    """Visibly charred ground of the 17 -> 22 Nov pair outside every stored scar (the definition of
    burn_res_replay.diagnose, at 20 m on raw reflectance), and how much of it each rule outlines."""
    pre_d, post_d = stac.day_number(PRE), stac.day_number(POST)
    s20 = (round((inner[3] - inner[1]) / 20), round((inner[2] - inner[0]) / 20))
    tf20 = from_origin(inner[0], inner[3], 20, 20)
    pre = {k: np.full(s20, np.nan, np.float32) for k in ("B8A", "B12", "valid")}
    post = {k: np.full(s20, np.nan, np.float32) for k in ("B02", "B03", "B04", "B06", "B07", "B08", "B8A", "B11", "B12", "valid")}
    if w.res == 20:  # the replay's own block reads
        for bid, by_day in diag.items():
            r, c = (int(x[1:]) for x in bid.split("_"))
            bx0, by1 = grid.x0 + c * 20, grid.y0 - r * 20
            r0, c0 = round((by1 - inner[3]) / 20), round((inner[0] - bx0) / 20)
            for day, dst in ((pre_d, pre), (post_d, post)):
                if day not in by_day:
                    continue
                src = by_day[day]
                h, wd = src["valid"].shape
                rs = slice(max(r0, 0), min(r0 + s20[0], h))
                cs = slice(max(c0, 0), min(c0 + s20[1], wd))
                drs = slice(rs.start - r0, rs.stop - r0)
                dcs = slice(cs.start - c0, cs.stop - c0)
                for k in dst:
                    dst[k][drs, dcs] = src[k][rs, cs].astype(np.float32)
        scope20 = np.isfinite(post["valid"])
    else:  # 40 m stage: the 20 m reads cached by burn_res_replay.py over the case's outer window
        key = next(p for p in (RES_CACHE / w.tile / "20m").iterdir()
                   if box(*[float(v) for v in p.name[1:].split("_")]).contains(box(*inner)))
        ox0, _, _, oy1 = (float(v) for v in key.name[1:].split("_"))
        r0, c0 = round((oy1 - inner[3]) / 20), round((inner[0] - ox0) / 20)
        crop = np.load(key / "cropland.npy")[r0:r0 + s20[0], c0:c0 + s20[1]]
        area20 = rasterize([job._from_lonlat(AREAS[w.stage], grid.epsg)], s20, tf20)
        xs = inner[0] + (np.arange(s20[1]) + 0.5) * 20
        ys = inner[3] - (np.arange(s20[0]) + 0.5) * 20
        cx0, cy0, cx1, cy1 = grid.core
        core20 = ((ys > cy0) & (ys <= cy1))[:, None] & ((xs >= cx0) & (xs < cx1))[None, :]
        for day, dst in ((pre_d, pre), (post_d, post)):
            with np.load(key / f"{day}.npz") as d:
                refl = {b: stac.reflectance(d[b], 1e-4, -0.1)[r0:r0 + s20[0], c0:c0 + s20[1]] for b in stac.REFLECTANCE_BANDS}
                scl = d["SCL"][r0:r0 + s20[0], c0:c0 + s20[1]]
            ok = crop & area20 & core20 & burnscar.valid_scl(scl) & np.isfinite(refl["B12"]) & np.isfinite(refl["B8A"])
            for k in dst:
                dst[k] = ok.astype(np.float32) if k == "valid" else refl[k]
        scope20 = np.ones(s20, bool)
    with np.errstate(invalid="ignore"):
        pv, qv = pre["valid"] == 1, post["valid"] == 1
        bais2 = burnscar.bais2(post)
        tbi = burnscar.tbi(post)
        visible = (qv & pv & scope20 & (bais2 >= VISIBLE["bais2_min"]) & (tbi <= VISIBLE["tbi_max"])
                   & (pre["B12"] - post["B12"] > VISIBLE["d_b12"]) & (pre["B8A"] - post["B8A"] > VISIBLE["d_b8a"]))
    region20 = rasterize([shapely.box(*inner)], s20, tf20)  # the inner window; the stage area is in valid
    scars = stored_scars(DB_URL, w.tile, grid.epsg, inner)
    stored = rasterize([job._from_lonlat(shapely.from_wkt(s["wkt"]), grid.epsg) for s in scars], s20, tf20)
    unmarked = visible & ~stored & region20
    on22 = {k: rasterize([c["geom_px"] for c in kept[k] if c["post_day"] == post_d], s20, tf20) for k in RULES}
    season20 = {k: rasterize([c["geom_px"] for c in kept[k]], s20, tf20) for k in RULES}

    def ha(m: np.ndarray) -> float:
        return round(float(m.sum()) * 0.04, 2)

    return {
        "definition": f"20 m raw reflectance, post BAIS2 >= {VISIBLE['bais2_min']}, TBI <= {VISIBLE['tbi_max']}, "
                      f"pre-post dB12 > {VISIBLE['d_b12']}, dB8A > {VISIBLE['d_b8a']}, clear on both passes, "
                      "WorldCover cropland, stage area, tile square, window; minus every stored scar (eval DB)",
        "visibly_charred_ha": ha(visible & region20), "visibly_charred_unmarked_ha": ha(unmarked),
        "unmarked_outlined_22nov": {k: ha(unmarked & on22[k]) for k in RULES},
        "unmarked_outlined_season": {k: ha(unmarked & season20[k]) for k in RULES},
        "unmarked_outlined_22nov_candidate_only": ha(unmarked & on22["cand"] & ~on22["base"]),
        "case_22nov_outline_ha": {k: ha(on22[k] & region20) for k in RULES},
    }


AREAS: dict[str, Any] = {}
DB_URL = DEFAULT_DB


def replay(args: argparse.Namespace) -> None:
    global DB_URL
    DB_URL = args.db
    with db_session(args.db) as session:
        areas, _ = job.stage_geometries(session)
    AREAS.update(areas)
    cache = args.cache or (args.out / "cache")
    names = set(args.windows) if args.windows else {w.name for w in WINDOWS}
    for w in WINDOWS:
        if w.name in names:
            replay_window(w, args.out, cache, areas)


def summary(args: argparse.Namespace) -> None:
    rows = []
    by_res: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for w in WINDOWS:
        p = args.out / w.name / "report.json"
        if not p.exists():
            continue
        r = json.loads(p.read_text())
        rows.append({k: r.get(k) for k in ("window", "tile", "res_m", "baseline_ha", "candidate_ha", "added_ha",
                                           "removed_ha", "added_share_of_baseline", "added_pieces_ge_min_size",
                                           "hazy_block_passes", "normalised_block_passes")}
                    | {"meets_10pct": (r["added_share_of_baseline"] or 0) >= 0.10,
                       "visible_17_22": r.get("visible_17_22")})
        t = r["timing"]
        for k in ("base_s", "cand_s", "block_passes", "save_base_s", "save_cand_s"):
            by_res[r["res_m"]][k] += t[k]
        by_res[r["res_m"]]["blocks"] += len(r["blocks"])
    extra = {}
    for res, t in by_res.items():
        extra[res] = {"per_block_pass_s": (t["cand_s"] - t["base_s"]) / t["block_passes"],
                      "per_block_save_s": (t["save_cand_s"] - t["save_base_s"]) / t["blocks"],
                      "block_passes_replayed": t["block_passes"]}
    # Production mixes 20 m (hotspots) and 40 m tiles; take the larger per-pass cost for all.
    per_bp = max(e["per_block_pass_s"] for e in extra.values())
    per_save = max(e["per_block_save_s"] for e in extra.values())
    est = {f: round(PROD["minutes"] + (per_bp * PROD["block_passes"] * f + per_save * PROD["blocks"]) / 60, 1)
           for f in (1.0, 1.25)}
    out = {"windows": rows, "bar_a": sum(r["meets_10pct"] for r in rows) >= 3,
           "bar_a_windows_meeting": sum(r["meets_10pct"] for r in rows),
           "runtime": {"extra_by_res": {str(k): {kk: round(v, 4) for kk, v in e.items()} for k, e in extra.items()},
                       "estimate_min_at_pass_factor": est, "production": PROD},
           "bar_d": all(v <= PROD["budget"] for v in est.values())}
    print(json.dumps(out, indent=1))
    (args.out / "summary.json").write_text(json.dumps(out, indent=1))


def chips(args: argparse.Namespace) -> None:
    raise SystemExit("chips: not written yet")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("out", type=Path)
    r.add_argument("windows", nargs="*")
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--cache", type=Path, default=None)
    s = sub.add_parser("summary")
    s.add_argument("out", type=Path)
    c = sub.add_parser("chips")
    c.add_argument("out", type=Path)
    c.add_argument("chips_dir", type=Path)
    c.add_argument("key", type=Path)
    c.add_argument("--db", default=DEFAULT_DB)
    c.add_argument("--cache", type=Path, default=None)
    args = ap.parse_args()
    {"replay": replay, "summary": summary, "chips": chips}[args.cmd](args)


if __name__ == "__main__":
    main()
