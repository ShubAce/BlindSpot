"""Evaluate seed and grow (s2burn-rules-4) against s2burn-rules-2 on six windows, with production code.

The candidate (analytics.burnscar, RULE.grow): pixels passing every strict test are seeds; pixels
passing looser tests of the same pass pair (dB8A > 0.04, dB11 > 0.04, dB12 > 0.02, BAIS2 >= 0.70,
brightness <= 0.40, post NBR < 0; same gap, re-arm, cropland and clear-sky rules) join a burn when
4-connected to a seed of that pass pair through such pixels; the 2x2 opening (20 m) and the minimum
cluster size stay, and the look-alike filter judges a cluster's seed pixels. The baseline is the
same code with grow=False (the per-pixel rule of s2burn-rules-1/-2, whose clusters are all seeds).

What one replay does, as the job does it (see scripts/burn_res_replay.py, whose helpers it uses):
it finds every Earth Search item of the tile from 1 October, reads each pass once over the window
plus a margin at the stage's production resolution (20 m in the hotspot districts, 40 m elsewhere),
judges only the stage's area on WorldCover cropland inside the tile's own 100 km square, and folds
the passes in date order into two block memories, one per rule (each with its own re-arming), with
burnscar.apply_pass; clusters come from the job's extract_parts, merge_parts, minimum size,
_clean_polygon and look-alike filter. Areas are counted in the inner window only (clusters are
judged on the whole outer window). Raw reads are cached per window (<cache>/<tile>/<res>m/<key>/),
and 20 m windows inside the full production blocks cached by the rules-3 replay are cut from them
(--block-cache), so reruns download nothing. Nothing is written to any database.

Differences from production: the window, not a 1024 px block, is the unit for the plausibility
gate, the haze flag, the 2x2 opening's edge padding and the growth (production cannot grow across a
block edge), so areas can differ slightly from stored scars.

Outputs in <out>/<name>/: report.json, clusters.json (kept clusters of both rules and the 'added'
clusters below, with lon/lat outlines and pass dates), arrays.npz (season masks, region, scope).

Run from services/api:
  uv run python scripts/burn_grow_eval.py replay <out> <name> <tile> <x0> <y0> <x1> <y1> --stage hotspots
      [--utm] [--margin-m 1000] [--cache DIR] [--block-cache DIR] [--case-id N --pair 2026-11-17 2026-11-22]
  uv run python scripts/burn_grow_eval.py runtime <block_cache> 43REP r4096_c1024 --out-json runtime.json
  uv run python scripts/burn_grow_eval.py summary <out> --prod-run run.json --runtime-json runtime.json

EVALUATION WINDOWS (production resolution per stage), fixed before any replay:
  W1 case 4723594 (Agwar Gujjran, Ludhiana) on 43REQ, hotspots, 20 m: the case box
     [75.44845, 30.820457, 75.461004, 30.831237] enlarged to ~3 x 3 km about its centre
     [75.43904, 30.81232, 75.47041, 30.83938]; pair 17 -> 22 Nov.
  W2 case 4936479 (Ratta Theh) box [75.70084, 29.681656, 75.741794, 29.71723] on 43REN, haryana, 40 m;
     pair 17 -> 22 Nov.
  W3 case 4939467 (Kalwan) box [75.970462, 29.698501, 75.998314, 29.722692] on 43REN, haryana, 40 m;
     pair 17 -> 22 Nov.
  W4 Fazilka [74.086948, 30.512422, 74.153693, 30.548355] on 43RDP, hotspots, 20 m.
  W5 Barnala UTM 43N [545000, 3362000, 555000, 3372000] on 43REP, hotspots, 20 m.
  W6 Tarn Taran UTM 43N [503000, 3480500, 513000, 3490500] on 43REQ, hotspots, 20 m (margin 900 m,
     so the outer window stays inside the cached production block).
  Development (not evaluation) windows, used only to check the code before the evaluation runs:
  Sirsa 43RDN, Kaithal 43RFP and Karnal 43RFN (40 m, the res replay's windows).

PER WINDOW (season masks from 1 Oct through the 22 Nov pass, pixels of kept clusters, production
grid, inner window inside the stage area and the tile's square): baseline ha, candidate ha, added ha
(candidate and not baseline) and its share of the baseline, removed ha (baseline and not candidate);
for W1-W3 the 'visibly charred, unmarked' area of the 17 -> 22 Nov pair (20 m: post BAIS2 >= 0.70,
TBI <= 0.40, pre-post dB12 > 0.02 and dB8A > 0.04 from the two reads, clear on both, cropland, inside
the stage area and the inner window, outside every stored scar of the eval database) and how much
of it the baseline replay and the candidate outline (clusters of the 22 Nov pass; also the season);
and the compute time of apply_pass per rule.

AUDIT STRATA (scripts/burn_grow_chips.py, seed 20261123, pooled over the six windows):
  added (30): 4-connected components of a candidate cluster's pixels outside the baseline season
     mask, at least the minimum cluster size (13 px at 20 m, 4 px at 40 m), anchor in an inner
     window; outlined on their own, on the candidate cluster's own pre and post passes.
  baseline (20): baseline (rules-2) replay clusters, anchor in an inner window.
  positive_control (10): stored production scars (eval database) anchored in an inner window with a
     FIRMS detection in their window (fire_matches > 0 or unseen = false).
  negative_control (10): cropland patches of the same shapes as the added and baseline clusters,
     moved to clear cropland of the same window and pass pair that did not darken (median dB12 and
     dB8A <= 0, and >= 90% of pixels below the grow cuts) away from every scar.

PRE-REGISTERED BAR (written 2026-11-23 before the first evaluation replay; committed with this file):
ship the candidate only if
 (a) added area >= 10% of baseline area in at least 3 of the 6 windows, AND
 (b) the blind audit's 'added' precision >= max(0.6, 'baseline' precision - 0.10), AND
 (c) the audit is valid: judges call >= 70% of positive controls burnt and >= 70% of negative
     controls not burnt (decided chips), AND
 (d) the daily sentinel run stays within SENTINEL_BUDGET_MIN = 100 min: today's 54.6 min (job_run
     2065) plus the extra apply_pass time per block-pass (runtime subcommand, a full 1024 px block
     of a development tile) times that run's block-passes, doubled for a slower runner.
The orchestrator's verifier applies (b) and (c); this script reports (a) and (d). A candidate that
misses any part is not shipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from rasterio import features
from shapely.geometry import box, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/: the res replay's helpers
import burn_res_replay as rr

from blindspot.analytics import burnscar
from blindspot.analytics.burnscar import RULE, BlockState, Part
from blindspot.jobs import sentinel as job
from blindspot.sources import stac

RULES = {"base": replace(RULE, grow=False, pixel_version="s2burn-rules-1", version="s2burn-rules-2"),
         "cand": RULE}
STAGE_RES = {s.name: s.res for s in job.STAGES}
BLOCK = job.BLOCK_PX


# --- reads ------------------------------------------------------------------------------------------


def from_blocks(block_cache: Path | None, fr: rr.Frame, day: int) -> stac.BlockRead | None:
    """The outer window cut from full 20 m production blocks cached by the rules-3 replay
    (<block_cache>/<tile>/r<row>_c<col>/<day>.npz), or None if any block it needs is missing."""
    if block_cache is None or fr.res != 20:
        return None
    r0, c0 = int(fr.window.row_off), int(fr.window.col_off)
    h, w = fr.shape
    pieces = []
    for br in range(r0 // BLOCK * BLOCK, r0 + h, BLOCK):
        for bc in range(c0 // BLOCK * BLOCK, c0 + w, BLOCK):
            path = block_cache / fr.grid.tile / f"r{br}_c{bc}" / f"{day}.npz"
            if not path.exists():
                return None
            with np.load(path) as d:
                pieces.append((br, bc, {k: d[k] for k in d.files}))
    items: list[str] = []
    for *_, d in pieces:
        for i in json.loads(bytes(d["items"]).decode()):
            if i not in items:
                items.append(i)
    dn = {b: np.zeros((h, w), pieces[0][2][b].dtype) for b in (*stac.REFLECTANCE_BANDS, "SCL")}
    index = np.full((h, w), 255, np.uint8)
    covered = np.zeros((h, w), bool)
    for br, bc, d in pieces:
        bh, bw = d["SCL"].shape
        rs, re_ = max(r0, br), min(r0 + h, br + bh)
        cs, ce = max(c0, bc), min(c0 + w, bc + bw)
        if rs >= re_ or cs >= ce:
            continue
        src = (slice(rs - br, re_ - br), slice(cs - bc, ce - bc))
        dst = (slice(rs - r0, re_ - r0), slice(cs - c0, ce - c0))
        for b, arr in dn.items():
            arr[dst] = d[b][src]
        own = json.loads(bytes(d["items"]).decode())
        remap = np.full(256, 255, np.uint8)
        remap[: len(own)] = [items.index(i) for i in own]
        index[dst] = remap[d["item_index"][src]]
        covered[dst] = True
    if not covered.all():
        return None
    return stac.BlockRead(dn, index, items, 0)


def read_pass(cache: Path, block_cache: Path | None, fr: rr.Frame, key: str, day: int,
              items: list[stac.SceneItem]) -> tuple[stac.BlockRead, float]:
    """A pass over the outer window: the window cache, else the block cache, else Earth Search."""
    if not (cache / fr.grid.tile / f"{fr.res}m" / key / f"{day}.npz").exists():
        got = from_blocks(block_cache, fr, day)
        if got is not None:
            return got, 0.0
    return rr.read_cached(cache, fr, key, day, items)


def cropland(cache: Path, block_cache: Path | None, fr: rr.Frame, key: str) -> np.ndarray:
    path = cache / fr.grid.tile / f"{fr.res}m" / key / "cropland.npy"
    if not path.exists() and block_cache is not None and fr.res == 20:
        r0, c0 = int(fr.window.row_off), int(fr.window.col_off)
        h, w = fr.shape
        out = np.zeros((h, w), bool)
        done = np.zeros((h, w), bool)
        for br in range(r0 // BLOCK * BLOCK, r0 + h, BLOCK):
            for bc in range(c0 // BLOCK * BLOCK, c0 + w, BLOCK):
                p = block_cache / fr.grid.tile / f"r{br}_c{bc}" / "cropland.npy"
                if not p.exists():
                    break
                m = np.load(p)
                rs, re_ = max(r0, br), min(r0 + h, br + m.shape[0])
                cs, ce = max(c0, bc), min(c0 + w, bc + m.shape[1])
                out[rs - r0:re_ - r0, cs - c0:ce - c0] = m[rs - br:re_ - br, cs - bc:ce - bc]
                done[rs - r0:re_ - r0, cs - c0:ce - c0] = True
        if done.all():
            path.parent.mkdir(parents=True, exist_ok=True)
            np.save(path, out)
    return rr.cropland(cache, fr, key)


# --- one rule's replay --------------------------------------------------------------------------------


@dataclass
class RuleRun:
    rule: burnscar.BurnRule
    state: BlockState
    registry: list[str] = field(default_factory=list)
    parts: dict[int, list[Part]] = field(default_factory=lambda: defaultdict(list))
    seeds: dict[int, np.ndarray] = field(default_factory=dict)  # post day -> seed pixels of that pass
    apply_s: float = 0.0
    passes: int = 0


def finish(run: RuleRun, fr: rr.Frame, sensing: dict[str, dt.datetime]) -> list[dict[str, Any]]:
    """The job's _finish_tile, minus the database (as burn_res_replay.finish)."""
    min_px = RULE.min_px(fr.res)
    kept = []
    for post_day, parts in sorted(run.parts.items()):
        for part in burnscar.merge_parts(parts):
            if part.n_px < min_px:
                continue
            poly = job._clean_polygon(part.geom, fr.res)
            means = {k: part.mean(k) for k in ("d_b8a", "d_b11", "d_b12", "dnbr", "nbr_post", "bais2_post",
                                                "tbi_post", "ndvi_pre", "ndvi_post")}
            if poly is None or not RULE.keeps(burnscar.seed_nbr_post(part)):
                continue
            pre_id = run.registry[part.mode("pre_item") or 0]
            seed_px = (part.tags.get("seed") or {}).get(1, 0)
            kept.append({
                "res": fr.res, "post_day": post_day,
                "pre": str(sensing[pre_id].date()) if pre_id in sensing else str(stac.from_day_number(part.key)),
                "post": str(stac.from_day_number(post_day)), "n_px": part.n_px,
                "area_ha": round(part.n_px * fr.res**2 / 10_000, 3), "seed_share": round(seed_px / part.n_px, 3),
                "hazy": bool(part.mode("hazy")), "geom_px": part.geom, "geom_stored": poly,
                "means": {k: (round(v, 4) if v is not None else None) for k, v in means.items()},
            })
    return kept


@dataclass
class Window:
    name: str
    tile: str
    stage: str
    res: int
    fr: rr.Frame
    key: str
    area_utm: Any
    passes: dict[int, list[stac.SceneItem]]
    footprints: dict[str, Any]
    sensing: dict[str, dt.datetime]
    outer: tuple[float, ...]
    inner: tuple[float, ...]
    epsg: int


def open_window(args: argparse.Namespace) -> Window:
    res = STAGE_RES[args.stage]
    area_ll = rr.stage_area(args.db, args.stage)
    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC)
    if args.utm:
        epsg = 32600 + int(args.tile[:2])
        inner_ll = box(*stac.utm_bounds_to_lonlat(epsg, tuple(args.bounds)))
    else:
        inner_ll = box(*args.bounds)
    scenes, _ = stac.search_scenes(mapping(inner_ll.buffer(0.02)), start, end)
    usable = [s for s in scenes if s.mgrs_tile == args.tile and (s.cloud_pct or 0) <= job.MAX_CLOUD_PCT
              and (s.nodata_pct or 0) < job.MAX_NODATA_PCT]
    passes: dict[int, list[stac.SceneItem]] = defaultdict(list)
    for s in usable:
        passes[stac.day_number(s.datetime)].append(s)
    grid40 = stac.TileGrid.from_scene(usable[0], 40)
    inner_utm = tuple(args.bounds) if args.utm else job._from_lonlat(inner_ll, grid40.epsg).envelope.bounds
    inner = rr.snap(grid40, inner_utm, 40)
    m = args.margin_m
    outer = rr.snap(grid40, (inner[0] - m, inner[1] - m, inner[2] + m, inner[3] + m), 40)
    key = "w" + "_".join(str(round(v)) for v in outer)
    area_utm = job._from_lonlat(area_ll, grid40.epsg).intersection(box(*grid40.core))
    return Window(args.name, args.tile, args.stage, res, rr.frame(usable[0], res, outer, inner), key, area_utm,
                  passes, {s.item_id: job._from_lonlat(shape(s.geometry), grid40.epsg) for s in usable},
                  {s.item_id: s.datetime for s in usable}, outer, inner, grid40.epsg)


def run_rules(win: Window, cache: Path, block_cache: Path | None, keep_days: set[int],
              ) -> tuple[dict[str, RuleRun], np.ndarray, dict[int, dict[str, Any]], float]:
    """Both rules over the window's passes on shared reads. Returns the runs, the scope, the reads
    kept for `keep_days` (reflectance and validity) and the seconds spent downloading."""
    fr = win.fr
    scope = rr.rasterize([win.area_utm], fr) & fr.grid.core_mask(fr.window)
    scope &= cropland(cache, block_cache, fr, win.key)
    runs = {name: RuleRun(rule, BlockState.empty(fr.shape)) for name, rule in RULES.items()}
    outer_box = box(*fr.bounds)
    todo = {d: [s for s in win.passes[d] if win.footprints[s.item_id].intersects(outer_box)] for d in sorted(win.passes)}
    kept: dict[int, dict[str, Any]] = {}
    read_s = 0.0
    with ThreadPoolExecutor(max_workers=job.PASS_READERS) as pool:
        futures = {d: pool.submit(read_pass, cache, block_cache, fr, win.key, d, items)
                   for d, items in todo.items() if items}
        for day in sorted(win.passes):
            if day not in futures:
                for run in runs.values():
                    run.state.applied_days.append(day)
                continue
            read, seconds = futures.pop(day).result()
            read_s += seconds
            items = {s.item_id: s for s in todo[day]}
            first = items.get(read.items[0], todo[day][0])
            refl = {b: stac.reflectance(read.dn[b], first.scale[b], first.offset[b]) for b in stac.REFLECTANCE_BANDS}
            for run in runs.values():
                t0 = time.perf_counter()
                out = burnscar.apply_pass(run.state, refl, read.dn["SCL"], read.items, read.item_index, day, scope,
                                          run.registry, run.rule, res_m=fr.res)
                run.apply_s += time.perf_counter() - t0
                run.state.applied_days.append(day)
                if not out.used:
                    continue
                run.passes += 1
                if out.burn is not None and out.burn.any():
                    ev = {k: np.where(out.burn, v, 0).astype(np.float32) for k, v in out.evidence.items()}
                    tags = {"pre_item": out.pre_item, "post_item": out.post_item,
                            "hazy": np.full(out.burn.shape, int(out.hazy), np.uint16),
                            "seed": out.seed.astype(np.uint16)}
                    run.parts[day].extend(burnscar.extract_parts(out.burn, out.pre_day, fr.transform, ev, tags))
                    run.seeds[day] = out.seed & out.burn
            if day in keep_days:
                valid = scope & burnscar.valid_scl(read.dn["SCL"])
                for b in ("B12", "B8A", "B11", "B04", "B08"):
                    valid &= np.isfinite(refl[b])
                kept[day] = {"refl": refl, "valid": valid}
            del read, refl
    return runs, scope, kept, read_s


# --- the 'visibly charred, unmarked' area of a pass pair (20 m) --------------------------------------


def visible_unmarked(win: Window, args: argparse.Namespace, cache: Path, block_cache: Path | None,
                     kept20: dict[int, dict[str, Any]] | None, pre: int, post: int,
                     ) -> tuple[rr.Frame, np.ndarray, np.ndarray, dict[str, float]]:
    """(20 m frame, visibly charred mask, its part outside every stored scar, notes), as the res
    replay's diagnosis defines them (burn_res_replay.VISIBLE)."""
    from sqlalchemy import text

    fr20 = win.fr if win.res == 20 else rr.frame(next(iter(win.passes.values()))[0], 20, win.outer, win.inner)
    if kept20 is None or pre not in kept20 or post not in kept20:
        scope20 = rr.rasterize([win.area_utm], fr20) & fr20.grid.core_mask(fr20.window)
        scope20 &= cropland(cache, block_cache, fr20, win.key)
        kept20 = {}
        for day in (pre, post):
            items = [s for s in win.passes[day] if win.footprints[s.item_id].intersects(box(*fr20.bounds))]
            read, _ = read_pass(cache, block_cache, fr20, win.key, day, items)
            by_id = {s.item_id: s for s in items}
            first = by_id.get(read.items[0], items[0])
            refl = {b: stac.reflectance(read.dn[b], first.scale[b], first.offset[b]) for b in stac.REFLECTANCE_BANDS}
            valid = scope20 & burnscar.valid_scl(read.dn["SCL"])
            for b in ("B12", "B8A", "B11", "B04", "B08"):
                valid &= np.isfinite(refl[b])
            kept20[day] = {"refl": refl, "valid": valid}
    a, b = kept20[pre], kept20[post]
    v = rr.VISIBLE
    with np.errstate(invalid="ignore"):
        db8a = a["refl"]["B8A"] - b["refl"]["B8A"]
        db12 = a["refl"]["B12"] - b["refl"]["B12"]
        look = (burnscar.bais2(b["refl"]) >= v["bais2_min"]) & (burnscar.tbi(b["refl"]) <= v["tbi_max"])
        dark = (db12 > v["d_b12"]) & (db8a > v["d_b8a"])
    region20 = region_mask(win, fr20)
    visible = look & dark & a["valid"] & b["valid"] & region20
    with rr.db_session(args.db) as session:
        rows = session.execute(text("""
            SELECT ST_AsText(geom) FROM burn_cluster
            WHERE mgrs_tile = :tile AND geom && ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326)"""),
            {"tile": win.tile, **dict(zip(("x0", "y0", "x1", "y1"),
                                          stac.utm_bounds_to_lonlat(win.epsg, tuple(win.outer)), strict=True))}).scalars().all()
    stored = rr.rasterize([job._from_lonlat(shapely.from_wkt(w), win.epsg) for w in rows], fr20)
    outside = visible & ~stored
    near = rr._ndimage_dilate(stored, 2)
    notes = {"stored_scars_in_outer_window": len(rows), "visibly_charred_ha": ha(visible, 20),
             "outside_every_stored_scar_ha": ha(outside, 20),
             "outside_within_40m_of_a_stored_scar_ha": ha(outside & near, 20)}
    return fr20, visible, outside, notes


def _ndimage() -> Any:
    from scipy import ndimage

    return ndimage


def region_mask(win: Window, fr: rr.Frame) -> np.ndarray:
    inner = np.zeros(fr.shape, bool)
    inner[fr.inner] = True
    return inner & rr.rasterize([win.area_utm], fr) & fr.grid.core_mask(fr.window)


def ha(mask: np.ndarray, res: int) -> float:
    return round(float(mask.sum()) * res * res / 10_000, 2)


def to20(mask: np.ndarray, res: int, shape20: tuple[int, int]) -> np.ndarray:
    return mask if res == 20 else rr.upsample(mask, shape20)


# --- replay -------------------------------------------------------------------------------------------


def added_clusters(cand: list[dict[str, Any]], base_season: np.ndarray, fr: rr.Frame, win: Window,
                   seeds: dict[int, np.ndarray]) -> list[dict[str, Any]]:
    """4-connected pieces of each candidate cluster outside the baseline season mask, at least the
    minimum cluster size, with the candidate cluster's own pass pair."""
    min_px = RULE.min_px(fr.res)
    inner_box = box(*win.inner)
    out = []
    for c in cand:
        own = rr.rasterize([c["geom_px"]], fr)
        only = own & ~base_season
        if only.sum() < min_px:
            continue
        labels, _n = _ndimage().label(only, structure=burnscar.FOUR_CONNECTED)
        seed = seeds.get(c["post_day"], np.zeros(fr.shape, bool))
        for geom, lab in features.shapes(labels.astype(np.int32), mask=labels > 0, transform=fr.transform,
                                         connectivity=4):
            piece = labels == int(lab)
            n_px = int(piece.sum())
            if n_px < min_px:
                continue
            g = shape(geom)
            out.append({
                "res": fr.res, "post_day": c["post_day"], "pre": c["pre"], "post": c["post"], "n_px": n_px,
                "area_ha": round(n_px * fr.res**2 / 10_000, 3), "seed_share": round(float((piece & seed).sum()) / n_px, 3),
                "parent_area_ha": c["area_ha"], "geom_px": g, "geom_stored": job._clean_polygon(g, fr.res) or g,
                "anchor_in_window": bool(inner_box.contains(job._anchor(g))), "means": c["means"],
            })
    return out


def replay(args: argparse.Namespace) -> None:
    out_dir = args.out / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / "cache")
    t_start = time.perf_counter()
    win = open_window(args)
    fr = win.fr
    pair = [stac.day_number(dt.date.fromisoformat(d)) for d in args.pair] if args.pair else None
    print(f"{win.name}: {win.tile} {win.stage} {win.res} m, inner {win.inner}, outer {win.outer}, "
          f"{len(win.passes)} passes {[str(stac.from_day_number(d)) for d in sorted(win.passes)]}", flush=True)
    keep_days = set(pair) if (pair and win.res == 20) else set()
    runs, scope, kept, read_s = run_rules(win, cache, args.block_cache, keep_days)
    clusters = {name: finish(run, fr, win.sensing) for name, run in runs.items()}
    season = {name: rr.rasterize([c["geom_px"] for c in cl], fr) for name, cl in clusters.items()}
    region = region_mask(win, fr)
    base, cand = season["base"] & region, season["cand"] & region
    base_ha, cand_ha = ha(base, win.res), ha(cand, win.res)
    added = added_clusters(clusters["cand"], season["base"], fr, win, runs["cand"].seeds)
    inner_box = box(*win.inner)
    for cl in clusters.values():
        for c in cl:
            c["anchor_in_window"] = bool(inner_box.contains(job._anchor(c["geom_stored"])))
    seed_px = sum(int((runs["cand"].seeds.get(c["post_day"], np.zeros(fr.shape, bool))
                       & rr.rasterize([c["geom_px"]], fr) & region).sum()) for c in clusters["cand"])
    mpx = fr.shape[0] * fr.shape[1] / 1e6
    report: dict[str, Any] = {
        "name": win.name, "tile": win.tile, "stage": win.stage, "res_m": win.res, "epsg": win.epsg,
        "inner_utm": win.inner, "outer_utm": win.outer, "bounds_arg": args.bounds, "utm": args.utm,
        "passes": [str(stac.from_day_number(d)) for d in sorted(win.passes)],
        "region_ha": ha(region, win.res), "cropland_ha_in_region": ha(scope & region, win.res),
        "baseline_ha": base_ha, "candidate_ha": cand_ha,
        "added_ha": ha(cand & ~base, win.res), "removed_ha": ha(base & ~cand, win.res),
        "added_share_of_baseline": round(ha(cand & ~base, win.res) / base_ha, 3) if base_ha else None,
        "candidate_seed_px_share": round(seed_px / max(int(cand.sum()), 1), 3),
        "clusters_in_window": {k: sum(c["anchor_in_window"] for c in v) for k, v in clusters.items()},
        "added_clusters_in_window": sum(c["anchor_in_window"] for c in added),
        "added_ha_in_auditable_clusters": round(sum(c["area_ha"] for c in added if c["anchor_in_window"]), 2),
        "timing": {"outer_mpx": round(mpx, 3), "read_download_s": round(read_s, 1),
                   **{f"apply_s_{k}": round(r.apply_s, 2) for k, r in runs.items()},
                   **{f"passes_used_{k}": r.passes for k, r in runs.items()},
                   "extra_apply_s_per_mpx_pass": round((runs["cand"].apply_s - runs["base"].apply_s)
                                                       / max(runs["cand"].passes, 1) / mpx, 4),
                   "base_apply_s_per_mpx_pass": round(runs["base"].apply_s / max(runs["base"].passes, 1) / mpx, 4)},
    }
    arrays: dict[str, np.ndarray] = {"season_base": season["base"], "season_cand": season["cand"], "region": region,
                                     "scope": scope}
    if pair and args.case_id:
        fr20, visible, outside, notes = visible_unmarked(win, args, cache, args.block_cache,
                                                        kept if win.res == 20 else None, *pair)
        pair_masks = {name: to20(rr.rasterize([c["geom_px"] for c in cl if c["post_day"] == pair[1]], fr), win.res,
                                 fr20.shape) for name, cl in clusters.items()}
        report["visible_pair"] = {
            "case_id": args.case_id, "pair": [str(stac.from_day_number(d)) for d in pair], **notes,
            **{f"outlined_by_{k}_pair_ha": ha(outside & m, 20) for k, m in pair_masks.items()},
            **{f"outlined_by_{k}_season_ha": ha(outside & to20(season[k], win.res, fr20.shape), 20)
               for k in season},
        }
        o = report["visible_pair"]
        report["visible_pair"]["candidate_pair_share"] = (round(o["outlined_by_cand_pair_ha"] / o["outside_every_stored_scar_ha"], 3)
                                                          if o["outside_every_stored_scar_ha"] else None)
        arrays.update(visible20=visible, visible_outside20=outside)
    report["seconds"] = round(time.perf_counter() - t_start, 1)
    print(json.dumps(report, indent=1), flush=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))

    def row(c: dict[str, Any], kind: str) -> dict[str, Any]:
        return {**{k: v for k, v in c.items() if k not in ("geom_px", "geom_stored")}, "kind": kind,
                "window": win.name, "tile": win.tile, "epsg": win.epsg, "geom_utm_wkt": c["geom_stored"].wkt,
                "geom_px_wkt": c["geom_px"].wkt, "geom_ll": mapping(job._to_lonlat(c["geom_stored"], win.epsg))}

    rows = [row(c, k) for k, cl in clusters.items() for c in cl] + [row(c, "added") for c in added]
    (out_dir / "clusters.json").write_text(json.dumps(rows))
    np.savez_compressed(out_dir / "arrays.npz", **arrays)
    print(f"wrote {out_dir} in {time.perf_counter() - t_start:.0f} s", flush=True)


# --- summary: bar (a) and (d) --------------------------------------------------------------------------


def summary(args: argparse.Namespace) -> None:
    """Bar (a) from the window reports; bar (d) from the runtime subcommand's extra apply_pass time
    per 1024 x 1024 px block-pass (mean, --runtime-json; a 40 m block has as many pixels as a 20 m
    one), times the block-passes of a production run (--prod-run: JSON of job_run tiles [{stage,
    blocks, passes}]), times 2 for a runner slower than this machine, added to today's minutes."""
    reps = [json.loads(p.read_text()) for p in sorted(args.out.glob("*/report.json"))]
    reps = [r for r in reps if not args.windows or r["name"] in args.windows]
    rows = [{"window": r["name"], "tile": r["tile"], "res_m": r["res_m"], "baseline_ha": r["baseline_ha"],
             "candidate_ha": r["candidate_ha"], "added_ha": r["added_ha"], "removed_ha": r["removed_ha"],
             "added_share_of_baseline": r["added_share_of_baseline"],
             "meets_10pct": (r["added_share_of_baseline"] or 0) >= 0.10,
             "window_apply_s": {k: v for k, v in r["timing"].items() if k.startswith("apply_s")},
             **({"visible_pair": r["visible_pair"]} if "visible_pair" in r else {})} for r in reps]
    out: dict[str, Any] = {"windows": rows, "windows_meeting_10pct": sum(r["meets_10pct"] for r in rows)}
    out["bar_a"] = out["windows_meeting_10pct"] >= 3
    if args.prod_run and args.runtime_json:
        run = json.loads(args.prod_run.read_text())
        rt = json.loads(args.runtime_json.read_text())
        extra = max(0.0, rt["mean_s_per_block_pass"]["cand"] - rt["mean_s_per_block_pass"]["base"])
        block_passes = sum(t["blocks"] * t["passes"] for t in run["tiles"])
        extra_min = block_passes * extra / 60
        out["runtime"] = {"prod_run": run.get("id"), "prod_run_minutes": run.get("minutes"),
                          "block_passes": block_passes, "extra_s_per_block_pass": round(extra, 4),
                          "extra_min_this_machine": round(extra_min, 2), "extra_min_x2": round(2 * extra_min, 2),
                          "today_min": args.today_min, "estimate_min": round(args.today_min + 2 * extra_min, 1),
                          "budget_min": args.budget_min}
        out["bar_d"] = args.today_min + 2 * extra_min <= args.budget_min
    print(json.dumps(out, indent=1))
    if args.out_json:
        args.out_json.write_text(json.dumps(out, indent=1))


# --- runtime: apply_pass on a full production block, both rules ---------------------------------------


def runtime(args: argparse.Namespace) -> None:
    """Seconds of apply_pass per block-pass with and without growth, on a full 1024 x 1024 px block
    cached by the rules-3 replay (all its passes, in date order, cropland as scope, Earth Search
    scale 1e-4 and offset -0.1), --repeat times; the medians per pass are reported."""
    folder = args.block_cache / args.tile / args.block
    days = sorted(int(p.stem) for p in folder.glob("*.npz"))
    scope = np.load(folder / "cropland.npy")
    per = {name: [] for name in RULES}
    for _ in range(args.repeat):
        states = {name: (BlockState.empty(scope.shape), []) for name in RULES}
        for day in days:
            with np.load(folder / f"{day}.npz") as d:
                dn = {b: d[b] for b in (*stac.REFLECTANCE_BANDS, "SCL")}
                index, items = d["item_index"], json.loads(bytes(d["items"]).decode())
            refl = {b: stac.reflectance(dn[b], 1e-4, -0.1) for b in stac.REFLECTANCE_BANDS}
            for name, rule in RULES.items():
                state, registry = states[name]
                t0 = time.perf_counter()
                burnscar.apply_pass(state, refl, dn["SCL"], items, index, day, scope, registry, rule, res_m=20)
                per[name].append(time.perf_counter() - t0)
    base, cand = (float(np.median(per[k])) for k in ("base", "cand"))
    out = {"block": f"{args.tile} {args.block}", "shape": list(scope.shape), "passes": len(days), "repeat": args.repeat,
           "median_s_per_block_pass": {"base": round(base, 4), "cand": round(cand, 4)},
           "mean_s_per_block_pass": {k: round(float(np.mean(v)), 4) for k, v in per.items()},
           "extra_s_per_block_pass": round(cand - base, 4)}
    print(json.dumps(out, indent=1))
    if args.out_json:
        args.out_json.write_text(json.dumps(out, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("out", type=Path)
    r.add_argument("name")
    r.add_argument("tile")
    r.add_argument("bounds", type=float, nargs=4, help="inner window, lon/lat (or tile UTM with --utm)")
    r.add_argument("--stage", choices=sorted(STAGE_RES), required=True)
    r.add_argument("--utm", action="store_true")
    r.add_argument("--margin-m", type=float, default=1000.0)
    r.add_argument("--db", default=rr.DEFAULT_DB)
    r.add_argument("--cache", type=Path, default=None)
    r.add_argument("--block-cache", type=Path, default=None)
    r.add_argument("--start", default="2026-10-01")
    r.add_argument("--end", default="2026-11-22T23:59:59")
    r.add_argument("--case-id", type=int, default=None)
    r.add_argument("--pair", nargs=2, default=None, help="pre and post date of the case's pass pair")
    s = sub.add_parser("summary")
    s.add_argument("out", type=Path)
    s.add_argument("--windows", nargs="*", default=None)
    s.add_argument("--prod-run", type=Path, default=None)
    s.add_argument("--runtime-json", type=Path, default=None)
    s.add_argument("--today-min", type=float, default=54.6)
    s.add_argument("--budget-min", type=float, default=100.0)
    s.add_argument("--out-json", type=Path, default=None)
    t = sub.add_parser("runtime")
    t.add_argument("block_cache", type=Path)
    t.add_argument("tile")
    t.add_argument("block", help="e.g. r4096_c1024")
    t.add_argument("--repeat", type=int, default=3)
    t.add_argument("--out-json", type=Path, default=None)
    args = ap.parse_args()
    {"replay": replay, "summary": summary, "runtime": runtime}[args.cmd](args)


if __name__ == "__main__":
    main()
