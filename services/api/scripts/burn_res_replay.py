"""Replay the production burn rule (s2burn-rules-2) over a window at 40 m and at 20 m, side by side.

Why: Haryana is mapped at 40 m (jobs/sentinel.py, stage 'haryana'), and on the case pages of
Fatehabad and Jind many fields that look burnt by eye carry no outline. This replays the job's own
per-pixel rule (analytics.burnscar.apply_pass) and cluster steps over a window from 1 October, once
at 40 m (as production maps Haryana today) and once at 20 m (as production maps Punjab's hotspot
districts), and says what 20 m would add, and why visibly charred ground is left unmarked.

What one replay does, per resolution, as the job does it: it finds every Earth Search item of the
tile, reads each pass (all items of a date, mosaicked by least nodata) over the window plus a margin
(Resampling.average, so 40 m comes from the same COG overviews production reads), judges only the
stage area (the 'haryana' stage polygon from the boundary table) on WorldCover cropland (mode
resampled to that resolution) inside the tile's own 100 km square, folds the passes in date order
into one per-pixel memory (BlockState) with apply_pass(res_m=res), and builds clusters with the
job's code: extract_parts, merge_parts, the minimum size (RULE.min_px: 4 px at 40 m, 13 at 20 m),
_clean_polygon and the look-alike filter (RULE.keeps). Differences from a production block: the
window (window + margin, not a 1024 px block) is the unit for the plausibility gate (median cropland
B12 within (0.02, 0.6)) and for the 2x2 opening's edge padding; in rules-2 haze changes only the
harvest bookkeeping and the confidence, never which pixels burn. Clusters are judged on the whole
outer window, areas are counted in the inner window only, so a field cut by the outer edge does not
bias the counts.

Outputs in <out>/<name>/: report.json (areas, counts, timing), clusters.json (kept clusters of both
resolutions, with how much of each is detected at the other resolution), arrays.npz (season masks,
and per-pixel evidence and memory at the --diag-post pass, for `diagnose`). Raw reads are cached
under <cache>/<tile>/<res>m/<window>/ (DN), so reruns cost no downloads. Nothing is written to any
database: provenance rows are switched off, and the eval database is opened read-only.

Run from services/api:
  uv run python scripts/burn_res_replay.py replay <out> <name> <tile> <minlon> <minlat> <maxlon> <maxlat>
      [--margin-m 1000] [--db URL] [--cache DIR] [--start 2026-10-01] [--end 2026-11-22T23:59:59]
      [--diag-pre 2026-11-17] [--diag-post 2026-11-22]
  uv run python scripts/burn_res_replay.py diagnose <out> <name> --case-id N [--db URL]
  uv run python scripts/burn_res_replay.py summary <out> [--key audit_key.json --verdicts verdicts.json]

PRE-REGISTERED BAR (written 2026-11-23, before any replay was run; committed with this file)
Ship 20 m for Haryana's high-burning districts only if all three hold:
 (a) AREA: in at least 3 of the 5 windows (two case boxes on 43REN: case 4936479 Ratta Theh,
     Fatehabad and case 4939467 Kalwan, Jind; one ~5 x 5 km window each in Sirsa, Kaithal and
     Karnal, centred on dense stored scars), the 20m_only area is >= 10% of the 40 m area.
     Season masks from 1 Oct through the 22 Nov pass, pixels of kept clusters, on the 20 m grid of
     the inner window, inside the Haryana stage area: 40 m area = 40 m pixels x 0.16 ha;
     20m_only = 20 m pixels not inside any 40 m detected pixel, x 0.04 ha.
 (b) BLIND AUDIT: 40 chips of the 'both' stratum and 40 of the '20m_only' stratum, drawn at random
     (random.Random(20261123), pooled over the five windows) from the kept 20 m clusters whose
     anchor lies in an inner window; 'both' = >= 50% of the cluster's 20 m pixels are detected at
     40 m in the season, '20m_only' = < 50%. Chips are judged blind to the stratum; 'burnt' counts,
     'uncertain' counts as not burnt. Ship only if precision(20m_only) >= max(0.6,
     precision(both) - 0.10).
 (c) RUNTIME: the daily run must start all tiles within SENTINEL_BUDGET_MIN = 100 with room. Estimate
     the tile loop (first tile start to last tile stored) at the 22 Nov pass counts from production
     job_run 1999 (22 Nov): per-tile seconds fitted as a x blocks + b x block-passes per resolution,
     store time per tile from source_fetch timestamps, Haryana tiles the budget skipped on 22 Nov
     scaled from job_run 1556 (17 Nov) by the 22/17 Nov ratio of the tiles both runs processed;
     then add the extra 20 m block-passes the chosen districts need and subtract the 40 m ones the
     Haryana stage no longer reads. Ship only if the estimate is <= 80 min at 22 Nov's pass counts
     and <= 100 min with 25% more passes (the rest of the season).
A change that misses any part is not shipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from rasterio import features
from rasterio.windows import Window
from shapely.geometry import box, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api: the blindspot package
from blindspot import provenance

provenance.record_fetch = lambda *a, **k: None  # a replay leaves no source_fetch rows anywhere

from blindspot.analytics import burnscar
from blindspot.analytics.burnscar import RULE, BlockState, Part
from blindspot.jobs import sentinel as job
from blindspot.sources import stac

RESOLUTIONS = (40, 20)
DEFAULT_DB = "postgresql+psycopg://blindspot:blindspot@localhost:5434/blindspot_eval"
# "Visibly charred" for the diagnosis (20 m, the pass pair read directly, not the memory).
VISIBLE = {"bais2_min": 0.70, "tbi_max": 0.40, "d_b12": 0.02, "d_b8a": 0.04}
BOTH_SHARE = 0.5  # a 20 m cluster is 'both' when at least this share of its pixels is detected at 40 m
OUTCOME = {1: "cluster too small", 2: "no clean polygon", 3: "look-alike filter (mean post NBR >= 0)", 4: "kept"}


def db_session(db_url: str) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(db_url, connect_args={"options": "-c default_transaction_read_only=on"})
    return Session(engine)


def stage_area(db_url: str, name: str = "haryana") -> Any:
    with db_session(db_url) as session:
        areas, _ = job.stage_geometries(session)
    return areas[name]


@dataclass
class Frame:
    """The outer window on one resolution's grid, and where the inner window sits in it."""

    res: int
    grid: stac.TileGrid
    window: Window
    inner: tuple[slice, slice]

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.window.height), int(self.window.width))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.grid.window_bounds(self.window)

    @property
    def transform(self) -> Any:
        return self.grid.window_transform(self.window)


def snap(grid: stac.TileGrid, bounds: tuple[float, float, float, float], step: int) -> tuple[float, ...]:
    """Bounds snapped outward to a `step` metre grid anchored at the tile's upper-left corner."""
    x0, y0, x1, y1 = bounds
    return (grid.x0 + math.floor((x0 - grid.x0) / step) * step, grid.y0 - math.ceil((grid.y0 - y0) / step) * step,
            grid.x0 + math.ceil((x1 - grid.x0) / step) * step, grid.y0 - math.floor((grid.y0 - y1) / step) * step)


def frame(scene: stac.SceneItem, res: int, outer: tuple[float, ...], inner: tuple[float, ...]) -> Frame:
    grid = stac.TileGrid.from_scene(scene, res)
    col, row = round((outer[0] - grid.x0) / res), round((grid.y0 - outer[3]) / res)
    w, h = round((outer[2] - outer[0]) / res), round((outer[3] - outer[1]) / res)
    r0, c0 = round((outer[3] - inner[3]) / res), round((inner[0] - outer[0]) / res)
    r1, c1 = round((outer[3] - inner[1]) / res), round((inner[2] - outer[0]) / res)
    return Frame(res, grid, Window(col, row, w, h), (slice(r0, r1), slice(c0, c1)))


def read_cached(cache: Path, fr: Frame, key: str, day: int, items: list[stac.SceneItem]) -> tuple[stac.BlockRead, float]:
    """One pass over the outer window at the frame's resolution: (read, seconds spent downloading)."""
    path = cache / fr.grid.tile / f"{fr.res}m" / key / f"{day}.npz"
    if path.exists():
        with np.load(path) as d:
            return stac.BlockRead({b: d[b] for b in (*stac.REFLECTANCE_BANDS, "SCL")}, d["item_index"],
                                  json.loads(bytes(d["items"]).decode()), 0), 0.0
    t0 = time.perf_counter()
    read = stac.read_pass_block(items, fr.bounds, fr.shape)
    seconds = time.perf_counter() - t0
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, item_index=read.item_index,
                        items=np.frombuffer(json.dumps(read.items).encode(), np.uint8), **read.dn)
    return read, seconds


def cropland(cache: Path, fr: Frame, key: str) -> np.ndarray:
    path = cache / fr.grid.tile / f"{fr.res}m" / key / "cropland.npy"
    if path.exists():
        return np.load(path)
    hrefs, _ = stac.worldcover_hrefs(stac.utm_bounds_to_lonlat(fr.grid.epsg, fr.bounds))
    classes = stac.read_worldcover(hrefs, fr.grid.epsg, fr.transform, fr.shape)
    mask = classes == stac.WORLDCOVER_CROPLAND
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, mask)
    return mask


def rasterize(geoms: list[Any], fr: Frame) -> np.ndarray:
    if not geoms:
        return np.zeros(fr.shape, bool)
    return features.rasterize([(g, 1) for g in geoms], out_shape=fr.shape, transform=fr.transform,
                              dtype=np.uint8).astype(bool)


@dataclass
class ResRun:
    """One resolution's replay: memory, parts per post pass, diagnostics, timing."""

    fr: Frame
    scope: np.ndarray
    state: BlockState
    registry: list[str] = field(default_factory=list)
    parts: dict[int, list[Part]] = field(default_factory=lambda: defaultdict(list))
    diag: dict[str, np.ndarray] = field(default_factory=dict)
    read_s: float = 0.0
    compute_s: float = 0.0
    passes_used: int = 0


def run_resolution(fr: Frame, key: str, cache: Path, area_utm: Any, passes: dict[int, list[stac.SceneItem]],
                   footprints: dict[str, Any], diag_pre: int | None, diag_post: int | None) -> ResRun:
    scope = rasterize([area_utm], fr) & fr.grid.core_mask(fr.window)
    scope &= cropland(cache, fr, key)
    run = ResRun(fr, scope, BlockState.empty(fr.shape))
    outer_box = box(*fr.bounds)
    days = sorted(passes)
    todo = {d: [s for s in passes[d] if footprints[s.item_id].intersects(outer_box)] for d in days}
    with ThreadPoolExecutor(max_workers=job.PASS_READERS) as pool:  # reads ahead, as the job does
        futures = {d: pool.submit(read_cached, cache, fr, key, d, items) for d, items in todo.items() if items}
        for day in days:
            if day not in futures:
                run.state.applied_days.append(day)
                continue
            read, seconds = futures.pop(day).result()
            run.read_s += seconds
            t0 = time.perf_counter()
            items = {s.item_id: s for s in todo[day]}
            first = items.get(read.items[0], todo[day][0])
            refl = {b: stac.reflectance(read.dn[b], first.scale[b], first.offset[b]) for b in stac.REFLECTANCE_BANDS}
            a = run.state.arrays
            before = {"last_day": a["last_day"].astype(np.int32), "burn_day": a["burn_day"].astype(np.int32)}
            out = burnscar.apply_pass(run.state, refl, read.dn["SCL"], read.items, read.item_index, day, scope,
                                      run.registry, res_m=fr.res)
            run.state.applied_days.append(day)
            valid = scope & burnscar.valid_scl(read.dn["SCL"]) & np.isfinite(refl["B12"]) & np.isfinite(refl["B8A"])
            valid &= np.isfinite(refl["B11"]) & np.isfinite(refl["B04"]) & np.isfinite(refl["B08"])
            if out.used:
                run.passes_used += 1
            if out.used and out.burn is not None and out.burn.any():
                ev = {k: np.where(out.burn, v, 0).astype(np.float32) for k, v in out.evidence.items()}
                tags = {"pre_item": out.pre_item, "post_item": out.post_item,
                        "hazy": np.full(out.burn.shape, int(out.hazy), np.uint16)}
                run.parts[day].extend(burnscar.extract_parts(out.burn, out.pre_day, fr.transform, ev, tags))
            if day == diag_pre:
                run.diag.update(pre_b8a=refl["B8A"].astype(np.float32), pre_b12=refl["B12"].astype(np.float32),
                                pre_valid=valid)
            if day == diag_post:
                ev = out.evidence
                run.diag.update(
                    post_b8a=refl["B8A"].astype(np.float32), post_b12=refl["B12"].astype(np.float32),
                    post_valid=valid, used=np.array(out.used),
                    **({k: ev[k].astype(np.float32) for k in ("d_b8a", "d_b11", "d_b12", "bais2_post", "tbi_post",
                                                              "nbr_post")} if out.used else {}),
                    last_day=before["last_day"], burn_day=before["burn_day"],
                    burn=out.burn if out.burn is not None else np.zeros(fr.shape, bool))
            run.compute_s += time.perf_counter() - t0
            del read, refl, out
    return run


def finish(run: ResRun, sensing: dict[str, dt.datetime], diag_post: int | None) -> list[dict[str, Any]]:
    """The job's _finish_tile, minus the database; also the per-pixel cluster outcome at diag_post."""
    t0 = time.perf_counter()
    fr = run.fr
    min_px = RULE.min_px(fr.res)
    kept: list[dict[str, Any]] = []
    outcome = np.zeros(fr.shape, np.uint8)
    for post_day, parts in sorted(run.parts.items()):
        for part in burnscar.merge_parts(parts):
            code = 4
            poly = None
            means = {k: part.mean(k) for k in ("d_b8a", "d_b11", "d_b12", "dnbr", "nbr_post", "bais2_post",
                                                "tbi_post", "ndvi_pre", "ndvi_post")}
            if part.n_px < min_px:
                code = 1
            elif (poly := job._clean_polygon(part.geom, fr.res)) is None:
                code = 2
            elif not RULE.keeps(means["nbr_post"]):
                code = 3
            if post_day == diag_post:
                outcome[rasterize([part.geom], fr)] = code
            if code != 4:
                continue
            pre_id = run.registry[part.mode("pre_item") or 0]
            kept.append({
                "res": fr.res, "post_day": post_day, "pre": str(sensing[pre_id].date()) if pre_id in sensing else None,
                "post": str(stac.from_day_number(post_day)), "n_px": part.n_px,
                "area_ha": round(part.n_px * fr.res**2 / 10_000, 3), "geom_px": part.geom, "geom_stored": poly,
                "means": {k: (round(v, 4) if v is not None else None) for k, v in means.items()},
            })
    if diag_post is not None:
        run.diag["outcome"] = outcome
    run.compute_s += time.perf_counter() - t0
    return kept


def upsample(mask40: np.ndarray, shape20: tuple[int, int]) -> np.ndarray:
    up = np.repeat(np.repeat(mask40, 2, axis=0), 2, axis=1)
    return up[: shape20[0], : shape20[1]]


def replay(args: argparse.Namespace) -> None:
    out_dir = args.out / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / "cache")
    t_start = time.perf_counter()
    area_ll = stage_area(args.db)
    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC)
    inner_ll = box(*args.bounds)
    scenes, _ = stac.search_scenes(mapping(inner_ll.buffer(0.02)), start, end)
    scenes = [s for s in scenes if s.mgrs_tile == args.tile]
    usable = [s for s in scenes if (s.cloud_pct or 0) <= job.MAX_CLOUD_PCT and (s.nodata_pct or 0) < job.MAX_NODATA_PCT]
    passes: dict[int, list[stac.SceneItem]] = defaultdict(list)
    for s in usable:
        passes[stac.day_number(s.datetime)].append(s)
    grid40 = stac.TileGrid.from_scene(usable[0], 40)
    inner_utm = job._from_lonlat(inner_ll, grid40.epsg).envelope.bounds
    inner = snap(grid40, inner_utm, 40)
    m = args.margin_m
    outer = snap(grid40, (inner[0] - m, inner[1] - m, inner[2] + m, inner[3] + m), 40)
    key = "w" + "_".join(str(round(v)) for v in outer)
    area_utm = job._from_lonlat(area_ll, grid40.epsg).intersection(box(*grid40.core))
    footprints = {s.item_id: job._from_lonlat(shape(s.geometry), grid40.epsg) for s in usable}
    sensing = {s.item_id: s.datetime for s in usable}
    diag_pre = stac.day_number(dt.date.fromisoformat(args.diag_pre)) if args.diag_pre else None
    diag_post = stac.day_number(dt.date.fromisoformat(args.diag_post)) if args.diag_post else None
    print(f"{args.name}: tile {args.tile}, inner {inner}, outer {outer}, {len(passes)} passes "
          f"{[str(stac.from_day_number(d)) for d in sorted(passes)]}", flush=True)

    runs: dict[int, ResRun] = {}
    kept: dict[int, list[dict[str, Any]]] = {}
    for res in RESOLUTIONS:
        fr = frame(usable[0], res, outer, inner)
        runs[res] = run_resolution(fr, key, cache, area_utm, passes, footprints, diag_pre, diag_post)
        kept[res] = finish(runs[res], sensing, diag_post)
        print(f"  {res} m: {len(kept[res])} kept clusters, read {runs[res].read_s:.1f} s, "
              f"compute {runs[res].compute_s:.1f} s", flush=True)

    fr40, fr20 = runs[40].fr, runs[20].fr
    season = {res: rasterize([c["geom_px"] for c in kept[res]], runs[res].fr) for res in RESOLUTIONS}
    up40 = upsample(season[40], fr20.shape)
    in_area20 = rasterize([area_utm], fr20) & fr20.grid.core_mask(fr20.window)
    inner_mask = np.zeros(fr20.shape, bool)
    inner_mask[fr20.inner] = True
    region = inner_mask & in_area20  # inner window, inside the stage area and the tile's square
    m20, m40 = season[20] & region, up40 & region
    ha20 = float(m20.sum()) * 0.04
    ha40 = float(m40.sum()) * 0.04  # = 40 m pixels x 0.16 ha
    only20 = float((m20 & ~m40).sum()) * 0.04
    only40 = float((m40 & ~m20).sum()) * 0.04
    both = float((m20 & m40).sum()) * 0.04
    inner_box = box(*inner)
    for res in RESOLUTIONS:
        # Share of each cluster's pixels (on the 20 m grid) detected at the other resolution.
        other = up40 if res == 20 else season[20]
        for c in kept[res]:
            c["anchor_in_window"] = bool(inner_box.contains(job._anchor(c["geom_stored"])))
            pix = rasterize([c["geom_px"]], fr20) if res == 20 else upsample(rasterize([c["geom_px"]], fr40), fr20.shape)
            c["share_other"] = round(float((pix & other).sum()) / max(int(pix.sum()), 1), 3)
    km2 = {res: runs[res].fr.shape[0] * runs[res].fr.shape[1] * res * res / 1e6 for res in RESOLUTIONS}
    report = {
        "name": args.name, "tile": args.tile, "inner_utm": inner, "outer_utm": outer, "epsg": grid40.epsg,
        "bounds_ll": args.bounds, "passes": [str(stac.from_day_number(d)) for d in sorted(passes)],
        "inner_km2": round((inner[2] - inner[0]) * (inner[3] - inner[1]) / 1e6, 2),
        "region_ha": round(float(region.sum()) * 0.04, 1),
        "cropland_ha_in_region_20m": round(float((runs[20].scope & region).sum()) * 0.04, 1),
        "detected_ha": {"40m": round(ha40, 1), "20m": round(ha20, 1)},
        "both_ha": round(both, 1), "only20_ha": round(only20, 1), "only40_ha": round(only40, 1),
        "only20_share_of_40m": round(only20 / ha40, 3) if ha40 else None,
        "clusters_in_window": {f"{res}m": sum(c["anchor_in_window"] for c in kept[res]) for res in RESOLUTIONS},
        "clusters_20m_in_window_by_stratum": {
            "both": sum(c["anchor_in_window"] and c["share_other"] >= BOTH_SHARE for c in kept[20]),
            "20m_only": sum(c["anchor_in_window"] and c["share_other"] < BOTH_SHARE for c in kept[20])},
        "timing": {f"{res}m": {"outer_km2": round(km2[res], 2), "passes_used": runs[res].passes_used,
                               "read_s": round(runs[res].read_s, 1), "compute_s": round(runs[res].compute_s, 1),
                               "read_s_per_km2": round(runs[res].read_s / km2[res], 4),
                               "compute_s_per_km2": round(runs[res].compute_s / km2[res], 4)}
                   for res in RESOLUTIONS},
        "seconds": round(time.perf_counter() - t_start, 1),
    }
    print(json.dumps(report, indent=1), flush=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    cl_out = []
    for res in RESOLUTIONS:
        for c in kept[res]:
            cl_out.append({
                **{k: v for k, v in c.items() if k not in ("geom_px", "geom_stored")}, "tile": args.tile,
                "epsg": grid40.epsg, "geom_utm_wkt": c["geom_stored"].wkt, "geom_px_wkt": c["geom_px"].wkt,
                "geom_ll": mapping(job._to_lonlat(c["geom_stored"], grid40.epsg)), "window": args.name,
            })
    (out_dir / "clusters.json").write_text(json.dumps(cl_out))
    arrays: dict[str, np.ndarray] = {"season20": season[20], "season40": season[40], "region20": region,
                                     "scope20": runs[20].scope, "scope40": runs[40].scope}
    for res in RESOLUTIONS:
        for k, v in runs[res].diag.items():
            arrays[f"d{res}_{k}"] = v
    np.savez_compressed(out_dir / "arrays.npz", **arrays)
    print(f"wrote {out_dir} in {time.perf_counter() - t_start:.0f} s", flush=True)


# --- diagnosis: why visibly charred ground in a case box is not outlined ---------------------------

TESTS = ("dB8A", "dB11", "dB12", "BAIS2", "TBI", "gap", "re-arm", "cropland", "not clear (SCL)")


def first_failure(d: dict[str, np.ndarray], res: int, day: int) -> np.ndarray:
    """Index into TESTS of the first production test each pixel fails, len(TESTS) if it passes all."""
    rule = RULE
    with np.errstate(invalid="ignore"):
        ok = [
            d["d_b8a"] > rule.d_b8a, d["d_b11"] > rule.d_b11, d["d_b12"] > rule.d_b12,
            d["bais2_post"] >= rule.bais2_min, d["tbi_post"] <= rule.tbi_max,
            (d["last_day"] > 0) & (day - d["last_day"] <= rule.max_gap_days),
            (d["burn_day"] == 0) | (day - d["burn_day"] > rule.rearm_days),
            d["scope"], d["post_valid"],
        ]
    out = np.full(ok[0].shape, len(TESTS), np.uint8)
    for i in reversed(range(len(TESTS))):
        out[~ok[i]] = i
    return out


def _ndimage_dilate(mask: np.ndarray, px: int) -> np.ndarray:
    from scipy import ndimage

    return ndimage.binary_dilation(mask, structure=np.ones((3, 3), bool), iterations=px)


def diagnose(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    out_dir = args.out / args.name
    rep = json.loads((out_dir / "report.json").read_text())
    z = dict(np.load(out_dir / "arrays.npz"))
    epsg, outer = rep["epsg"], rep["outer_utm"]
    post = stac.day_number(dt.date.fromisoformat(args.diag_post))
    pre = stac.day_number(dt.date.fromisoformat(args.diag_pre))
    shape20 = z["season20"].shape
    tf20 = features.Affine(20, 0, outer[0], 0, -20, outer[3])
    with db_session(args.db) as session:
        rows = session.execute(text("""
            SELECT id, burn_after, burn_before, scope, res_m, ST_AsText(geom) AS wkt FROM burn_cluster
            WHERE mgrs_tile = :tile AND geom && ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326)"""),
            {"tile": rep["tile"], **dict(zip(("x0", "y0", "x1", "y1"), stac.utm_bounds_to_lonlat(epsg, tuple(outer)),
                                             strict=True))}).mappings().all()
    own = next((r for r in rows if r["id"] == args.case_id), None)
    if own is None:
        raise SystemExit(f"case {args.case_id} not found in the eval database near this window")

    def ras(sel: list[Any]) -> np.ndarray:
        geoms = [job._from_lonlat(shapely.from_wkt(r["wkt"]), epsg) for r in sel]
        if not geoms:
            return np.zeros(shape20, bool)
        return features.rasterize([(g, 1) for g in geoms], out_shape=shape20, transform=tf20, dtype=np.uint8).astype(bool)

    post_date, pre_date = stac.from_day_number(post), stac.from_day_number(pre)
    same = [r for r in rows if r["burn_before"].date() == post_date and r["id"] != args.case_id]
    earlier = [r for r in rows if r["burn_before"].date() < post_date]
    later = [r for r in rows if r["burn_before"].date() > post_date]
    m_own, m_same, m_earlier, m_later = ras([own]), ras(same), ras(earlier), ras(later)
    any_scar = m_own | m_same | m_earlier | m_later
    region = z["region20"]
    d20 = {k[4:]: v for k, v in z.items() if k.startswith("d20_")}
    d40 = {k[4:]: v for k, v in z.items() if k.startswith("d40_")}
    if "post_b8a" not in d20 or "pre_b8a" not in d20:
        raise SystemExit("the replay has no diagnostics for this pass pair (rerun replay with --diag-pre/--diag-post)")
    with np.errstate(invalid="ignore"):
        db8a = d20["pre_b8a"] - d20["post_b8a"]
        db12 = d20["pre_b12"] - d20["post_b12"]
        char_look = (d20["bais2_post"] >= VISIBLE["bais2_min"]) & (d20["tbi_post"] <= VISIBLE["tbi_max"])
        darkened = (db12 > VISIBLE["d_b12"]) & (db8a > VISIBLE["d_b8a"])
    clear = d20["pre_valid"] & d20["post_valid"]  # both passes clear, on 20 m cropland in the stage area
    visible = char_look & darkened & clear & region

    def ha(mask: np.ndarray) -> float:
        return round(float(mask.sum()) * 0.04, 2)

    outside = visible & ~any_scar
    near_scar = _ndimage_dilate(any_scar, 2)
    looks = char_look & d20["post_valid"] & region
    dark_before = looks & ~any_scar & ~(darkened & d20["pre_valid"])
    cand40_ever = upsample(z["d40_burn_day"] > 0, shape20) | upsample(z["d40_burn"], shape20)
    # 40 m: each 20 m pixel takes its 40 m parent's test results.
    d40["scope"] = z["scope40"]
    d20["scope"] = z["scope20"]
    f40 = upsample(first_failure(d40, 40, post), shape20)
    oc40 = upsample(d40["outcome"], shape20)
    f20 = first_failure(d20, 20, post)
    oc20 = d20["outcome"]
    cand20 = f20 == len(TESTS)

    def breakdown(f: np.ndarray, oc: np.ndarray, burn: np.ndarray | None, res: int) -> dict[str, float]:
        out = {f"fails {t}": ha(outside & (f == i)) for i, t in enumerate(TESTS)}
        passed = outside & (f == len(TESTS))
        if burn is not None:  # 20 m: the 2x2 opening runs before clustering
            out["removed by the 2x2 opening"] = ha(passed & ~burn)
            passed = passed & burn
        for code, label in OUTCOME.items():
            name = f"{label} at {res} m" if code != 4 else f"kept at {res} m (replay)"
            out[name] = ha(passed & (oc == code))
        out["other"] = round(ha(outside) - sum(out.values()), 2)
        return out

    rearm40 = outside & (f40 == TESTS.index("re-arm"))
    burnt_day40 = upsample(d40["burn_day"], shape20)
    report = {
        "case_id": args.case_id, "window": args.name, "pass_pair": [str(pre_date), str(post_date)],
        "visible_definition": f"20 m, post BAIS2 >= {VISIBLE['bais2_min']}, TBI <= {VISIBLE['tbi_max']}, "
                              f"pre-post dB12 > {VISIBLE['d_b12']}, dB8A > {VISIBLE['d_b8a']}, clear on both passes, "
                              "WorldCover cropland (20 m), inside the Haryana stage area, inner box",
        "stored_scars_near": {"same_window": len(same) + 1, "earlier": len(earlier), "later": len(later)},
        "visibly_charred_ha": ha(visible),
        "visible_by_stored_scar": {
            "inside the case's own scar": ha(visible & m_own),
            "inside another scar of the same window": ha(visible & ~m_own & m_same),
            "inside an earlier window's scar": ha(visible & ~m_own & ~m_same & m_earlier),
            "inside a later window's scar": ha(visible & ~m_own & ~m_same & ~m_earlier & m_later),
            "outside every stored scar": ha(outside),
        },
        "outside_first_failure_40m": breakdown(f40, oc40, None, 40),
        "outside_first_failure_20m": breakdown(f20, oc20, d20["burn"], 20),
        "outside_rearm_40m": {
            "re-arm is the first failure at 40 m": ha(rearm40),
            "of which the earlier 40 m burn pixel lies in a stored earlier scar": ha(rearm40 & m_earlier),
            "earliest such earlier burn": (str(stac.from_day_number(int(burnt_day40[rearm40].min())))
                                           if rearm40.any() else None),
            "latest such earlier burn": (str(stac.from_day_number(int(burnt_day40[rearm40].max())))
                                         if rearm40.any() else None),
        },
        "outside_20m_would_map_ha": ha(outside & cand20 & d20["burn"] & (oc20 == 4)),
        "outside_near_a_stored_scar_ha": {
            "within 40 m of a stored scar (field edges, strips)": ha(outside & near_scar),
            "farther (fields of their own)": ha(outside & ~near_scar),
        },
        "char_looking_on_post_pass_ha": {
            "all (BAIS2/TBI only, clear, cropland)": ha(looks),
            "inside the case's own scar": ha(looks & m_own),
            "inside another scar of the same window": ha(looks & ~m_own & m_same),
            "inside an earlier window's scar (burnt before the pre pass)": ha(looks & ~m_own & ~m_same & m_earlier),
            "outside, darkened since the pre pass (= visibly charred)": ha(outside),
            "outside, not darkened since the pre pass (dark before it)": ha(dark_before),
            "of which ever a 40 m burn candidate this season (cluster dropped)": ha(dark_before & cand40_ever),
            "of which mapped by the 20 m replay this season": ha(dark_before & z["season20"]),
        },
        "stored_scars_in_box_ha": ha(any_scar & region),
        "replay_season_vs_stored_40m": {
            "replay 40 m season ha in region": ha(upsample(z["season40"], shape20) & region),
            "stored scars (any window) ha in region": ha(any_scar & region),
        },
    }
    print(json.dumps(report, indent=1))
    (out_dir / f"diagnosis_{args.case_id}.json").write_text(json.dumps(report, indent=1))


# --- runtime estimate for bar (c) ------------------------------------------------------------------


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def stage_blocks(db_url: str, hotspots: dict[tuple[str, str], tuple[str, ...]],
                 grids: dict[str, stac.TileGrid]) -> dict[tuple[str, str], int]:
    """(stage, tile) -> production blocks read (1024 px windows meeting the stage area in the tile's
    square), for tiles the stage covers by >= MIN_STAGE_OVERLAP_M2, as run_sentinel/process_tile."""
    saved = job.HOTSPOT_DISTRICTS
    job.HOTSPOT_DISTRICTS = hotspots
    try:
        with db_session(db_url) as session:
            areas, _ = job.stage_geometries(session)
    finally:
        job.HOTSPOT_DISTRICTS = saved
    out = {}
    for stage in job.STAGES:
        for tile, g20 in grids.items():
            grid = stac.TileGrid(g20.tile, g20.epsg, stage.res, g20.x0, g20.y0, int(g20.width * 20 / stage.res),
                                 int(g20.height * 20 / stage.res), g20.core)
            area_utm = job._from_lonlat(areas[stage.name], grid.epsg).intersection(box(*grid.core))
            if area_utm.area < job.MIN_STAGE_OVERLAP_M2:
                continue
            shapely.prepare(area_utm)
            out[(stage.name, tile)] = sum(area_utm.intersects(box(*grid.window_bounds(w)))
                                          for w in grid.blocks(job.BLOCK_PX))
    return out


def runtime(args: argparse.Namespace) -> None:
    """Bar (c): the daily tile loop now and with the chosen Haryana districts at 20 m (see the
    docstring). Production figures come from `prod`, a JSON export of job_run 1999 (22 Nov) and
    1556 (17 Nov), the s2_cog fetch times of run 1999, and pass days per tile from s2_scene."""
    prod = json.loads(args.prod.read_text())
    run22, run17 = prod["runs"]["1999"], prod["runs"]["1556"]
    fits = {}
    for res, stages in ((20, ("hotspots",)), (40, ("punjab", "haryana"))):
        rows = [t for t in run22["tiles"] if t["stage"] in stages]
        x = np.array([[t["blocks"], t["blocks"] * t["passes"]] for t in rows], float)
        coef, *_ = np.linalg.lstsq(x, np.array([t["seconds"] for t in rows]), rcond=None)
        fits[res] = (float(coef[0]), float(coef[1]))
    fetches = prod["fetches_1999"]
    gaps = [(_ts(b["started_at"]) - _ts(a["finished_at"])).total_seconds()
            for a, b in itertools.pairwise(fetches)]
    store_s = float(np.mean(gaps))  # storing a tile: from its read's end to the next tile's start
    catalogue_s = (_ts(fetches[0]["started_at"]) - _ts(run22["started_at"])).total_seconds()
    loop22_s = (_ts(fetches[-1]["finished_at"]) - _ts(fetches[0]["started_at"])).total_seconds() + store_s
    done22 = {(t["stage"], t["tile"]) for t in run22["tiles"]}
    both = [(t17, t22) for t17 in run17["tiles"] for t22 in run22["tiles"]
            if t17["stage"] == t22["stage"] == "haryana" and t17["tile"] == t22["tile"]]
    ratio = sum(b["seconds"] for _, b in both) / sum(a["seconds"] for a, _ in both)
    skipped = [t for t in run17["tiles"] if t["stage"] == "haryana" and (t["stage"], t["tile"]) not in done22]
    skipped_s = sum(t["seconds"] * ratio + store_s for t in skipped)
    baseline_s = catalogue_s + loop22_s + skipped_s

    scenes, _ = stac.search_scenes(mapping(box(73.8, 27.6, 77.7, 32.6)), dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
                                   dt.datetime(2026, 11, 22, 23, 59, tzinfo=dt.UTC))
    grids: dict[str, stac.TileGrid] = {}
    for s in scenes:
        grids.setdefault(s.mgrs_tile, stac.TileGrid.from_scene(s, 20))
    passes = prod["pass_days"]
    # Run 1999 (22 Nov) ran with the 11 hotspot districts of before 23 Nov (the first 11 entries):
    # counting its blocks with that list checks the block counter against what production read.
    then = stage_blocks(args.db, dict(list(job.HOTSPOT_DISTRICTS.items())[:11]), grids)
    ran = {(t["stage"], t["tile"]): t["blocks"] for t in run22["tiles"] + skipped}
    check = {f"{k[0]}:{k[1]}": [then.get(k), v] for k, v in ran.items() if then.get(k) != v}
    check.update({f"{k[0]}:{k[1]}": [n, None] for k, n in then.items() if k not in ran})
    now = stage_blocks(args.db, job.HOTSPOT_DISTRICTS, grids)

    def extra(old: dict, new: dict) -> tuple[dict[str, float], list]:
        """Minutes added going from blocks `old` to `new` (per pass factor), and what changed."""
        out = {1.0: 0.0, 1.25: 0.0}
        changed = []
        for key in sorted(set(old) | set(new)):
            stage, tile = key
            a, b = fits[next(s.res for s in job.STAGES if s.name == stage)]
            delta = new.get(key, 0) - old.get(key, 0)
            if not delta:
                continue
            store = store_s * ((key in new) - (key in old))
            for f in out:
                out[f] += delta * (a + b * passes[tile] * f) + store
            changed.append([stage, tile, old.get(key, 0), new.get(key, 0), passes[tile]])
        return {str(f): round(v / 60, 1) for f, v in out.items()}, changed

    main_extra, main_changed = extra(then, now)
    configs = {}
    for k in args.top:
        chosen = args.districts[:k]
        new = stage_blocks(args.db, {**job.HOTSPOT_DISTRICTS, **{("Haryana", d): (d.lower(),) for d in chosen}}, grids)
        more, changed = extra(now, new)
        configs[f"top{k}"] = {"districts": chosen, "changed_stage_tile_blocks_now_new_passes": changed,
                              "extra_min": more}

    def scaled(f: float) -> float:
        """The baseline with f times the passes: each tile's modelled b x block-passes share grows."""
        total = catalogue_s + store_s * (len(run22["tiles"]) + len(skipped))
        for t, mult in [(t, 1.0) for t in run22["tiles"]] + [(t, ratio) for t in skipped]:
            a, b = fits[20 if t["stage"] == "hotspots" else 40]
            model = a * t["blocks"] + b * t["blocks"] * t["passes"]
            grown = a * t["blocks"] + b * t["blocks"] * t["passes"] * f
            total += t["seconds"] * mult * (grown / model if model > 0 else 1.0)
        return total

    run_config = {"1.0": round(baseline_s / 60, 1), "1.25": round(scaled(1.25) / 60, 1)}
    # main today: run 1999's list plus the seven Punjab districts moved to 20 m on 23 Nov
    base = {f: round(v + main_extra[f], 1) for f, v in run_config.items()}
    report = {
        "fits_s_per_block_and_per_block_pass": {f"{r}m": [round(v, 3) for v in c] for r, c in fits.items()},
        "store_s_per_tile": round(store_s, 1), "catalogue_s": round(catalogue_s, 1),
        "loop_22nov_actual_min": round(loop22_s / 60, 1), "haryana_ratio_22_over_17": round(ratio, 3),
        "skipped_haryana_tiles_22nov": [t["tile"] for t in skipped], "skipped_est_min": round(skipped_s / 60, 1),
        "loop_min_run_1999_config": run_config, "block_count_mismatches_vs_production": check,
        "main_23nov_punjab_move": {"extra_min": main_extra, "changed": main_changed},
        "baseline_loop_min_main": base,
        "configs": {k: {**v, "loop_min_at_pass_factor": {f: round(base[f] + v["extra_min"][f], 1) for f in base},
                        "bar_c_pass": bool(base["1.0"] + v["extra_min"]["1.0"] <= 80
                                           and base["1.25"] + v["extra_min"]["1.25"] <= 100)}
                    for k, v in configs.items()},
    }
    print(json.dumps(report, indent=1))
    if args.out_json:
        args.out_json.write_text(json.dumps(report, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("out", type=Path)
    r.add_argument("name")
    r.add_argument("tile")
    r.add_argument("bounds", type=float, nargs=4, help="inner window, lon/lat")
    r.add_argument("--margin-m", type=float, default=1000.0)
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--cache", type=Path, default=None)
    r.add_argument("--start", default="2026-10-01")
    r.add_argument("--end", default="2026-11-22T23:59:59")
    r.add_argument("--diag-pre", default="2026-11-17")
    r.add_argument("--diag-post", default="2026-11-22")
    d = sub.add_parser("diagnose")
    d.add_argument("out", type=Path)
    d.add_argument("name")
    d.add_argument("--case-id", type=int, required=True)
    d.add_argument("--db", default=DEFAULT_DB)
    d.add_argument("--diag-pre", default="2026-11-17")
    d.add_argument("--diag-post", default="2026-11-22")
    t = sub.add_parser("runtime")
    t.add_argument("prod", type=Path, help="JSON export of the production runs (see runtime())")
    t.add_argument("--db", default=DEFAULT_DB)
    t.add_argument("--districts", nargs="+", default=["Fatehabad", "Sirsa", "Kaithal", "Jind", "Karnal", "Hisar",
                                                         "Kurukshetra", "Bhiwani", "Hansi"])
    t.add_argument("--top", type=int, nargs="+", default=[5, 7, 9])
    t.add_argument("--out-json", type=Path, default=None)
    args = ap.parse_args()
    {"replay": replay, "diagnose": diagnose, "runtime": runtime}[args.cmd](args)


if __name__ == "__main__":
    main()
