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
      [--shared-cache DIR ...] [--res-cache DIR]
  uv run python scripts/burn_haze_eval.py chips <out> <chips_dir> <key_json> [same cache options]
  uv run python scripts/burn_haze_eval.py summary <out>
  uv run python scripts/burn_haze_eval.py links <out>    # case links across the rule change
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
import shapely.affinity
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
# Read-only caches of earlier replays, reused instead of reading the scenes again (set by main from
# --shared-cache and --res-cache):
SHARED_CACHES: list[Path] = []  # 20 m production blocks, <tile>/r<row>_c<col>/<day>.npz
RES_CACHE: Path | None = None  # 20 m windows of the 43REN cases (burn_res_replay.py); the 40 m windows need it
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
        # applied with a non-zero offset somewhere: a pass whose every fitted cell was too hazy is
        # "applied" with zero offsets, i.e. judged raw
        "corrected_block_passes": sum(bool(x.get("applied")) and (x.get("b02_offset_max") or 0) > 0
                                      for x in norm_log),
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
        if RES_CACHE is None:
            raise SystemExit(f"{w.name} is a 40 m window: give --res-cache (burn_res_replay.py's 20 m reads)")
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


def links(args: argparse.Namespace) -> None:
    """Case links across the rule change, from the replays' clusters (all blocks of each window). A
    rules-2 scar keeps its id when a rules-4h scar has its identity (pre and post pass, outline:
    jobs/sentinel.py IDENTITY); else jobs/sentinel.py _FORWARD_SQL forwards it to the rules-4h scar
    of the same post pass that overlaps it most, else (since the fallback) to one of another post
    pass within rearm_days covering at least FORWARD_MOVED_SHARE of it; else its link answers 404."""
    out: dict[str, Any] = {"forward_moved_share": job.FORWARD_MOVED_SHARE, "rearm_days": RULE.rearm_days}
    total: dict[str, int] = defaultdict(int)
    for w in WINDOWS:
        p = args.out / w.name / "clusters.json"
        if not p.exists():
            continue
        cl = json.loads(p.read_text())["clusters"]
        base = [c for c in cl if c["rule"] == "base"]
        cand = [c for c in cl if c["rule"] == "cand"]
        geoms = [shapely.from_wkt(c["geom"]) for c in cand]
        tree = shapely.STRtree(geoms)
        same_id = {(c["pre"], c["post"], c["geom"]) for c in cand}
        n = {"rules2_scars": len(base), "kept_id": 0, "forwarded_same_pass": 0, "forwarded_moved_pass": 0,
             "lost_moved_below_share": 0, "lost_no_overlap_within_rearm": 0}
        for b in base:
            if (b["pre"], b["post"], b["geom"]) in same_id:
                n["kept_id"] += 1
                continue
            g = shapely.from_wkt(b["geom"])
            post = dt.date.fromisoformat(b["post"])
            same = moved = 0.0
            for i in tree.query(g):
                share = geoms[i].intersection(g).area / g.area if g.area else 0.0
                days = abs((dt.date.fromisoformat(cand[i]["post"]) - post).days)
                if days == 0:
                    same = max(same, share)
                elif days <= RULE.rearm_days:
                    moved = max(moved, share)
            if same > 0:
                n["forwarded_same_pass"] += 1
            elif moved >= job.FORWARD_MOVED_SHARE:
                n["forwarded_moved_pass"] += 1
            elif moved > 0:
                n["lost_moved_below_share"] += 1
            else:
                n["lost_no_overlap_within_rearm"] += 1
        n["lost_before_fallback"] = len(base) - n["kept_id"] - n["forwarded_same_pass"]
        n["lost_after_fallback"] = n["lost_before_fallback"] - n["forwarded_moved_pass"]
        out[w.name] = n
        for k, v in n.items():
            total[k] += v
    out["total"] = dict(total)
    (args.out / "links.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


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
                                           "hazy_block_passes", "normalised_block_passes",
                                           "corrected_block_passes")}
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


# --- audit chips -----------------------------------------------------------------------------------

CHIP_RES = 10  # metres per chip pixel
CHIP_HALF_M = 600  # a ~1.2 km square
UPSCALE = 3
N_STRATA = {"added": 30, "baseline": 20, "positive_control": 10, "negative_control": 10}
NOT_DARK = {"d_b8a": 0.02, "d_b11": 0.02, "d_b12": 0.01}  # a negative control did not darken


def chip_box(geom: Any) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = geom.bounds
    cx, cy = round((x0 + x1) / 40) * 20, round((y0 + y1) / 40) * 20  # whole 20 m pixels
    half = round(max(CHIP_HALF_M, (x1 - x0) / 2 + 150, (y1 - y0) / 2 + 150) / 20) * 20
    return cx - half, cy - half, cx + half, cy + half


def read_10m(items: list[stac.SceneItem], bounds: tuple[float, ...], bands: tuple[str, ...]) -> dict[str, np.ndarray]:
    """DN over `bounds` at 10 m for one pass (its items mosaicked, least nodata first)."""
    from rasterio.enums import Resampling

    shp = (round((bounds[3] - bounds[1]) / CHIP_RES), round((bounds[2] - bounds[0]) / CHIP_RES))
    out: dict[str, np.ndarray] = {}
    for item in sorted(items, key=lambda s: s.nodata_pct if s.nodata_pct is not None else 100.0):
        got = {b: stac._read_one(item.hrefs[b], bounds, shp, Resampling.bilinear) for b in bands}
        has = got["B04"] > 0
        for b, arr in got.items():
            dst = out.setdefault(b, np.zeros(shp, arr.dtype))
            take = has & (dst == 0)
            dst[take] = arr[take]
    return out


def render_chip(geom: Any, pre: dt.date, post: dt.date, by_day: dict[dt.date, list[stac.SceneItem]],
                path: Path) -> list[float]:
    """Four panels of one square: true colour before / after, SWIR false colour before / after,
    the audited area in yellow on each, the dates in small captions; nothing else."""
    from PIL import Image, ImageDraw, ImageFont

    from blindspot.sources import pcdata

    bounds = chip_box(geom)
    views = {d: read_10m(by_day[d], bounds, ("B04", "B03", "B02", "B12", "B8A")) for d in (pre, post)}
    panels = []
    for kind in ("truecolor", "swir"):
        for d, word in ((pre, "before"), (post, "after")):
            v = views[d]
            bands = [v["B04"], v["B03"], v["B02"]] if kind == "truecolor" else [v["B12"], v["B8A"], v["B04"]]
            panels.append((pcdata.colourise(kind, bands)[..., :3], f"{word}  {d}" + ("  (SWIR)" if kind == "swir" else "")))
    rings = [geom.exterior, *geom.interiors] if geom.geom_type == "Polygon" else [g.exterior for g in geom.geoms]
    px = [[((x - bounds[0]) / CHIP_RES * UPSCALE, (bounds[3] - y) / CHIP_RES * UPSCALE) for x, y in r.coords]
          for r in rings]
    size = panels[0][0].shape[1] * UPSCALE, panels[0][0].shape[0] * UPSCALE
    gap, cap = 8, 18
    canvas = Image.new("RGB", (2 * size[0] + gap, 2 * (size[1] + cap) + gap), (255, 255, 255))
    font = ImageFont.load_default()
    for i, (rgb, text) in enumerate(panels):
        img = Image.fromarray(rgb).resize(size, Image.NEAREST)
        draw = ImageDraw.Draw(img)
        for ring in px:
            draw.line([*ring, ring[0]], fill=(255, 255, 0), width=2)
        x, y = (i % 2) * (size[0] + gap), (i // 2) * (size[1] + cap + gap)
        canvas.paste(img, (x, y))
        ImageDraw.Draw(canvas).text((x + 4, y + size[1] + 3), text, fill=(0, 0, 0), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, optimize=True)
    return list(bounds)


def _date_of(item_id: str) -> dt.date:
    stamp = item_id.split("_")[2]  # S2B_T43REQ_20261117T054316_L2A
    return dt.date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))


def _block_file(cache: Path, w: Win, b: Window, day: int) -> Path | None:
    shared = [c / w.tile / f"r{b.row_off}_c{b.col_off}" / f"{day}.npz" for c in SHARED_CACHES] if w.res == 20 else []
    return next((p for p in [*shared, cache_path(cache, w.tile, w.res, b, day).with_suffix(".npz")] if p.exists()), None)


def window_pair(w: Win, grid: stac.TileGrid, inner: tuple[float, ...], cache: Path, pre: int, post: int,
                area_utm: Any) -> np.ndarray | None:
    """On the window grid (stage resolution): cropland in scope, clear on both passes, not darkened."""
    shp = (round((inner[3] - inner[1]) / w.res), round((inner[2] - inner[0]) / w.res))
    ok = np.zeros(shp, bool)
    for b in grid.blocks(job.BLOCK_PX):
        bb = grid.window_bounds(b)
        if not box(*bb).intersects(box(*inner)):
            continue
        reads = {}
        for day in (pre, post):
            path = _block_file(cache, w, b, day)
            if path is None:
                return None
            with np.load(path) as d:
                reads[day] = ({x: stac.reflectance(d[x], 1e-4, -0.1) for x in ("B8A", "B11", "B12")}, d["SCL"])
        scope = rasterize([area_utm], (b.height, b.width), grid.window_transform(b)) & grid.core_mask(b)
        scope &= cropland(cache, w.tile, w.res, grid, b)
        (r1, s1), (r2, s2) = reads[pre], reads[post]
        with np.errstate(invalid="ignore"):
            good = (scope & burnscar.valid_scl(s1) & burnscar.valid_scl(s2)
                    & (r1["B8A"] - r2["B8A"] <= NOT_DARK["d_b8a"]) & (r1["B11"] - r2["B11"] <= NOT_DARK["d_b11"])
                    & (r1["B12"] - r2["B12"] <= NOT_DARK["d_b12"]))
        r0, c0 = round((bb[3] - inner[3]) / w.res), round((inner[0] - bb[0]) / w.res)
        rs = slice(max(-r0, 0), min(shp[0], b.height - r0))
        cs = slice(max(-c0, 0), min(shp[1], b.width - c0))
        ok[rs, cs] |= good[rs.start + r0:rs.stop + r0, cs.start + c0:cs.stop + c0]
    return ok


def chips(args: argparse.Namespace) -> None:
    global DB_URL
    DB_URL = args.db
    cache = args.cache or (args.out / "cache")
    rng = random.Random(SEED)
    with db_session(args.db) as session:
        areas, _ = job.stage_geometries(session)
    pools: dict[str, list[dict[str, Any]]] = {k: [] for k in N_STRATA}
    ctx: dict[str, dict[str, Any]] = {}
    for w in WINDOWS:
        cj = json.loads((args.out / w.name / "clusters.json").read_text())
        inner, epsg = tuple(cj["inner"]), cj["epsg"]
        ctx[w.name] = {"w": w, "inner": inner, "epsg": epsg}
        for i, a in enumerate(cj["added"]):
            pools["added"].append({"window": w.name, "id": f"{w.name}-added-{i}", "geom": a["geom"], "pre": a["pre"],
                                   "post": a["post"], "area_ha": a["area_ha"]})
        for i, c in enumerate(x for x in cj["clusters"] if x["rule"] == "base" and x["anchor_in_window"]):
            pools["baseline"].append({"window": w.name, "id": f"{w.name}-base-{i}", "geom": c["geom"], "pre": c["pre"],
                                      "post": c["post"], "area_ha": c["area_ha"]})
        for s in stored_scars(args.db, w.tile, epsg, inner):
            if not ((s["fire_matches"] or 0) > 0 or s["unseen"] is False):
                continue
            g = job._from_lonlat(shapely.from_wkt(s["wkt"]), epsg)
            if not box(*inner).contains(job._anchor(g)):
                continue
            pools["positive_control"].append({"window": w.name, "id": f"scar-{s['id']}", "geom": g.wkt,
                                              "pre": str(_date_of(s["scene_pre"])), "post": str(_date_of(s["scene_post"])),
                                              "area_ha": float(s["area_ha"])})
    picked: dict[str, list[dict[str, Any]]] = {}
    for k in ("added", "baseline", "positive_control"):
        pool = sorted(pools[k], key=lambda c: c["id"])
        rng.shuffle(pool)
        picked[k] = pool[: N_STRATA[k]]
    scenes_by_tile: dict[str, dict[dt.date, list[stac.SceneItem]]] = {}
    grids: dict[str, stac.TileGrid] = {}
    for w in WINDOWS:
        if w.tile in scenes_by_tile:
            continue
        lls = [stac.utm_bounds_to_lonlat(ctx[x.name]["epsg"], ctx[x.name]["inner"]) for x in WINDOWS if x.tile == w.tile]
        bbox = (min(b[0] for b in lls) - 0.05, min(b[1] for b in lls) - 0.05, max(b[2] for b in lls) + 0.05,
                max(b[3] for b in lls) + 0.05)
        items = search(w.tile, bbox)
        by_day: dict[dt.date, list[stac.SceneItem]] = defaultdict(list)
        for it in items:
            by_day[it.datetime.date()].append(it)
        scenes_by_tile[w.tile] = by_day
        grids[w.tile] = stac.TileGrid.from_scene(items[0], 20)
    # Negative controls: a random baseline cluster's outline and pass pair, moved to cropland of the
    # same window that was clear on both passes, did not darken, and lies in no scar (stored or replayed).
    negs: list[dict[str, Any]] = []
    shapes = sorted(pools["baseline"], key=lambda c: c["id"])
    rng.shuffle(shapes)
    for c in shapes:
        if len(negs) >= N_STRATA["negative_control"]:
            break
        x = ctx[c["window"]]
        w, inner = x["w"], x["inner"]
        g20 = grids[w.tile]
        grid = stac.TileGrid(g20.tile, g20.epsg, w.res, g20.x0, g20.y0, int(g20.width * 20 / w.res),
                             int(g20.height * 20 / w.res), g20.core)
        area_utm = job._from_lonlat(areas[w.stage], grid.epsg).intersection(box(*grid.core))
        ok = window_pair(w, grid, inner, cache, stac.day_number(dt.date.fromisoformat(c["pre"])),
                         stac.day_number(dt.date.fromisoformat(c["post"])), area_utm)
        if ok is None:
            continue
        tf = from_origin(inner[0], inner[3], w.res, w.res)
        masks = np.load(args.out / w.name / "masks.npz")
        stored = [job._from_lonlat(shapely.from_wkt(s["wkt"]), grid.epsg) for s in stored_scars(args.db, w.tile, grid.epsg, inner)]
        ok &= ~(masks["base"] | masks["cand"] | rasterize(stored, ok.shape, tf))
        g = shapely.from_wkt(c["geom"])
        gx0, gy0, gx1, gy1 = g.bounds
        for _ in range(300):
            dx = rng.uniform(inner[0] - gx0, inner[2] - gx1)
            dy = rng.uniform(inner[1] - gy0, inner[3] - gy1)
            moved = shapely.affinity.translate(g, round(dx / w.res) * w.res, round(dy / w.res) * w.res)
            pix = rasterize([moved], ok.shape, tf)
            if pix.sum() >= RULE.min_px(w.res) and (pix & ok).sum() >= 0.95 * pix.sum():
                negs.append({"window": w.name, "id": f"neg-{c['id']}", "geom": moved.wkt, "pre": c["pre"],
                             "post": c["post"], "area_ha": c["area_ha"]})
                break
    picked["negative_control"] = negs
    entries = [{"stratum": k, **c} for k, v in picked.items() for c in v]
    random.Random(SEED + 1).shuffle(entries)
    args.chips_dir.mkdir(parents=True, exist_ok=True)

    def one(i_e: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        i, e = i_e
        name = f"chip_{i + 1:03d}.png"
        tile = ctx[e["window"]]["w"].tile
        box_utm = render_chip(shapely.from_wkt(e["geom"]), dt.date.fromisoformat(e["pre"]),
                              dt.date.fromisoformat(e["post"]), scenes_by_tile[tile], args.chips_dir / name)
        return {"chip": name, "path": str((args.chips_dir / name).resolve()), **e, "chip_utm": box_utm, "tile": tile}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        key = list(pool.map(one, enumerate(entries)))
    summary_ = {"counts": {k: len(v) for k, v in picked.items()}, "pools": {k: len(v) for k, v in pools.items()}}
    args.key.write_text(json.dumps({"seed": SEED, **summary_, "chips": key}, indent=1))
    print(json.dumps(summary_))


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
    lk = sub.add_parser("links")
    lk.add_argument("out", type=Path)
    c = sub.add_parser("chips")
    c.add_argument("out", type=Path)
    c.add_argument("chips_dir", type=Path)
    c.add_argument("key", type=Path)
    c.add_argument("--db", default=DEFAULT_DB)
    c.add_argument("--cache", type=Path, default=None)
    for p in (r, c):
        p.add_argument("--shared-cache", type=Path, action="append", default=[],
                       help="a read-only cache of 20 m block reads (<tile>/r<row>_c<col>/<day>.npz); repeatable")
        p.add_argument("--res-cache", type=Path, default=None,
                       help="burn_res_replay.py's read-only cache of the 43REN case windows (40 m windows)")
    args = ap.parse_args()
    global SHARED_CACHES, RES_CACHE
    SHARED_CACHES, RES_CACHE = list(getattr(args, "shared_cache", [])), getattr(args, "res_cache", None)
    {"replay": replay, "summary": summary, "chips": chips, "links": links}[args.cmd](args)


if __name__ == "__main__":
    main()
