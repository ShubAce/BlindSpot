"""Replay the burn-scar rule over a window, old (s2burn-rules-2) and new (current RULE) side by side.

What it does, as the sentinel job does it (jobs/sentinel.py): it finds every Earth Search item of one
MGRS tile from 1 October, reads each pass over every production block (1024 x 1024 px at 20 m) that
touches the window, judges only the stage's districts on WorldCover cropland in the tile's own
100 km square, folds the passes in date order into two block memories (one per rule) with
analytics.burnscar.apply_pass, and builds clusters with the job's own code: extract_parts per
block, merge_parts across blocks, the minimum size, and the mean post-burn NBR filter (keeps).
Haze is judged per block as in production, so a window gets the same haze flags as the job.

It also runs the looser test of the earlier replay on the old rule's evidence (dB12 > 0.02,
dB8A > 0.04, BAIS2 >= 0.70, TBI <= 0.40, previous view at most 16 days old, no burn by the old
rule in the last 20 days) to find burn-like patches neither rule keeps, and records for every pixel when it first darkened in SWIR (dB11, dB12
past the rule's cuts within 6 days) and whether that pass was hazy. Per pass and block it notes the
haze flag, the blue rise behind it, the dark-target haze estimate (burnscar.dark_target_haze) and
whether the new rule deferred there.

Outputs (in <out>/<name>/): report.json (areas in the window and per-pass haze), clusters.json
(every kept cluster of both rules and every loose-only cluster, lon/lat outlines, dates and items),
masks.npz (per-pass masks over the window). Raw reads are cached in <cache> (DN, about 10 MB per
block and pass), so a rerun costs no downloads. It writes nothing to any database: provenance rows
are switched off, and the stage area comes from a read-only connection.

Run from services/api:
  uv run python scripts/burn_replay.py <out> <name> <tile> <minx> <miny> <maxx> <maxy> [--lonlat]
      [--db postgresql://...] [--cache DIR] [--start 2026-10-01] [--end 2026-11-23]
The window is in the tile's UTM metres, or lon/lat with --lonlat.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from rasterio import features
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import box, mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api: the blindspot package
import blindspot.provenance as provenance  # noqa: E402

provenance.record_fetch = lambda *a, **k: None  # a replay leaves no source_fetch rows anywhere

from blindspot.analytics import burnscar  # noqa: E402
from blindspot.analytics.burnscar import RULE, BlockState, Part  # noqa: E402
from blindspot.jobs import sentinel as job  # noqa: E402
from blindspot.sources import stac  # noqa: E402

OLD = replace(RULE, defer_hazy=False, pixel_version="s2burn-rules-1", version="s2burn-rules-2")
NEW = RULE
RULES = {"old": OLD, "new": NEW}
LOOSE = {"d_b12": 0.02, "d_b8a": 0.04, "bais2_min": 0.70, "tbi_max": 0.40, "max_gap_days": 16}
DEFAULT_DB = "postgresql+psycopg://blindspot:blindspot@localhost:5434/blindspot_eval"
STAGE = next(s for s in job.STAGES if s.name == "hotspots")


@dataclass
class RuleRun:
    """One rule's memory over the blocks, and the parts its passes produced."""

    rule: burnscar.BurnRule
    states: dict[str, BlockState] = field(default_factory=dict)
    registry: list[str] = field(default_factory=list)
    parts: dict[int, list[Part]] = field(default_factory=lambda: defaultdict(list))


def stage_area(db_url: str) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(db_url, connect_args={"options": "-c default_transaction_read_only=on"})
    with Session(engine) as session:
        areas, _ = job.stage_geometries(session)
    engine.dispose()
    return areas[STAGE.name]


def read_cached(cache: Path, tile: str, window: Window, day: int, items: list[stac.SceneItem],
                grid: stac.TileGrid) -> stac.BlockRead:
    path = cache / tile / f"r{window.row_off}_c{window.col_off}" / f"{day}.npz"
    if path.exists():
        with np.load(path) as d:
            return stac.BlockRead({b: d[b] for b in (*stac.REFLECTANCE_BANDS, "SCL")}, d["item_index"],
                                  json.loads(bytes(d["items"]).decode()), 0)
    read = stac.read_pass_block(items, grid.window_bounds(window), (window.height, window.width))
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, item_index=read.item_index,
                        items=np.frombuffer(json.dumps(read.items).encode(), np.uint8), **read.dn)
    return read


def cropland(cache: Path, grid: stac.TileGrid, window: Window) -> np.ndarray:
    path = cache / grid.tile / f"r{window.row_off}_c{window.col_off}" / "cropland.npy"
    if path.exists():
        return np.load(path)
    hrefs, _ = stac.worldcover_hrefs(stac.utm_bounds_to_lonlat(grid.epsg, grid.window_bounds(window)))
    classes = stac.read_worldcover(hrefs, grid.epsg, grid.window_transform(window), (window.height, window.width))
    mask = classes == stac.WORLDCOVER_CROPLAND
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, mask)
    return mask


def clusters(run: RuleRun, grid: stac.TileGrid, filtered: bool,
             sensing: dict[str, dt.datetime]) -> list[dict[str, Any]]:
    """The job's _finish_tile, minus the database: stitch, minimum size, look-alike filter."""
    out = []
    min_px = RULE.min_px(STAGE.res)
    for post_day, parts in sorted(run.parts.items()):
        for part in burnscar.iter_large(burnscar.merge_parts(parts), min_px):
            if job._clean_polygon(part.geom, STAGE.res) is None:
                continue
            means = {k: part.mean(k) for k in ("d_b8a", "d_b11", "d_b12", "dnbr", "nbr_post", "bais2_post",
                                                "tbi_post", "ndvi_pre", "ndvi_post")}
            if filtered and not RULE.keeps(means["nbr_post"]):
                continue
            reg = run.registry
            pre_item = job._earliest_pre_item(part, reg, sensing)
            out.append({
                "post_day": post_day, "pre_day": stac.day_number(sensing[pre_item]), "key_day": part.key,
                "n_px": part.n_px, "geom_utm": part.geom, "pre_item": pre_item,
                "post_item": reg[part.mode("post_item") or 0],
                "hazy_post": bool(part.mode("hazy")), "deferred_px": int(part.sums.get("deferred", 0)),
                "means": {k: (round(v, 4) if v is not None else None) for k, v in means.items()},
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("name")
    ap.add_argument("tile")
    ap.add_argument("bounds", type=float, nargs=4)
    ap.add_argument("--lonlat", action="store_true")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--start", default="2026-10-01")
    ap.add_argument("--end", default="2026-11-23")
    args = ap.parse_args()
    out_dir = args.out / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / "cache")
    t0 = time.perf_counter()

    area_ll = stage_area(args.db)
    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC)
    # One search to learn the tile grid, then one over the blocks the window touches.
    probe = args.bounds if args.lonlat else None
    if probe is None:
        from pyproj import Transformer
        to_ll = Transformer.from_crs(32600 + int(args.tile[:2]), 4326, always_xy=True)
        lon0, lat0 = to_ll.transform(args.bounds[0], args.bounds[1])
        lon1, lat1 = to_ll.transform(args.bounds[2], args.bounds[3])
        probe = (lon0, lat0, lon1, lat1)
    scenes, _ = stac.search_scenes(mapping(box(*probe)), start, end)
    scenes = [s for s in scenes if s.mgrs_tile == args.tile]
    grid = stac.TileGrid.from_scene(scenes[0], STAGE.res)
    if args.lonlat:
        win_utm = job._from_lonlat(box(*args.bounds), grid.epsg).envelope
    else:
        win_utm = box(*args.bounds)
    # Snap the window outward to the tile's pixel grid.
    wx0 = grid.x0 + math.floor((win_utm.bounds[0] - grid.x0) / grid.res) * grid.res
    wx1 = grid.x0 + math.ceil((win_utm.bounds[2] - grid.x0) / grid.res) * grid.res
    wy1 = grid.y0 - math.floor((grid.y0 - win_utm.bounds[3]) / grid.res) * grid.res
    wy0 = grid.y0 - math.ceil((grid.y0 - win_utm.bounds[1]) / grid.res) * grid.res
    win_box = box(wx0, wy0, wx1, wy1)
    win_shape = (round((wy1 - wy0) / grid.res), round((wx1 - wx0) / grid.res))
    win_tf = from_origin(wx0, wy1, grid.res, grid.res)
    area_utm = job._from_lonlat(area_ll, grid.epsg).intersection(box(*grid.core))
    blocks = [w for w in grid.blocks(job.BLOCK_PX)
              if win_box.intersects(box(*grid.window_bounds(w))) and area_utm.intersects(box(*grid.window_bounds(w)))]
    blocks_ll = stac.utm_bounds_to_lonlat(grid.epsg, shapely.union_all(
        [box(*grid.window_bounds(w)) for w in blocks]).bounds)
    scenes, _ = stac.search_scenes(mapping(box(*blocks_ll)), start, end)
    usable = [s for s in scenes if s.mgrs_tile == args.tile and (s.cloud_pct or 0) <= job.MAX_CLOUD_PCT
              and (s.nodata_pct or 0) < job.MAX_NODATA_PCT]
    passes: dict[int, list[stac.SceneItem]] = defaultdict(list)
    for s in usable:
        passes[stac.day_number(s.datetime)].append(s)
    days = sorted(passes)
    footprints = {s.item_id: job._from_lonlat(shapely.geometry.shape(s.geometry), grid.epsg) for s in usable}
    print(f"{args.name}: tile {args.tile}, window {win_shape} px, blocks {[job._block_id(w) for w in blocks]}, "
          f"{len(days)} passes {[str(stac.from_day_number(d)) for d in days]}", flush=True)

    runs = {k: RuleRun(rule) for k, rule in RULES.items()}
    loose = RuleRun(OLD, registry=runs["old"].registry)  # its parts carry the old run's item tags
    n = len(days)
    per_pass: list[dict[str, Any]] = []
    # Per-pass masks over the window (bool, pass x row x col) and first-darkening strata per pixel.
    m = {k: np.zeros((n, *win_shape), bool) for k in ("old_burn", "new_burn", "new_deferred", "new_opened", "loose")}
    first_dark = np.zeros(win_shape, np.int32)
    first_dark_hazy = np.zeros(win_shape, bool)
    first_loose = np.zeros(win_shape, np.int32)
    first_loose_hazy = np.zeros(win_shape, bool)
    win_scope = np.zeros(win_shape, bool)
    for w in blocks:
        bid = job._block_id(w)
        bshape = (w.height, w.width)
        btf = grid.window_transform(w)
        scope = features.rasterize([(area_utm, 1)], out_shape=bshape, transform=btf, dtype=np.uint8).astype(bool)
        scope &= grid.core_mask(w)
        if scope.any():
            scope &= cropland(cache, grid, w)
        # Where the block sits in the window grid.
        bx0, _by0, _bx1, by1 = grid.window_bounds(w)
        r0, c0 = round((wy1 - by1) / grid.res), round((bx0 - wx0) / grid.res)
        rs, cs = slice(max(r0, 0), min(r0 + w.height, win_shape[0])), slice(max(c0, 0), min(c0 + w.width, win_shape[1]))
        brs, bcs = slice(rs.start - r0, rs.stop - r0), slice(cs.start - c0, cs.stop - c0)
        win_scope[rs, cs] |= scope[brs, bcs]
        for run in (*runs.values(), loose):
            run.states[bid] = BlockState.empty(bshape)
        bbox = box(*grid.window_bounds(w))
        for i, day in enumerate(days):
            items = [s for s in passes[day] if footprints[s.item_id].intersects(bbox)]
            if not items or not scope.any():
                continue
            read = read_cached(cache, args.tile, w, day, items, grid)
            by_id = {s.item_id: s for s in items}
            first = by_id.get(read.items[0], items[0])
            refl = {b: stac.reflectance(read.dn[b], first.scale[b], first.offset[b]) for b in stac.REFLECTANCE_BANDS}
            old_state = runs["old"].states[bid].arrays
            last_day_before = old_state["last_day"].astype(np.int32).copy()
            burn_day_before = old_state["burn_day"].astype(np.int32).copy()
            res = {}
            for k, run in runs.items():
                res[k] = burnscar.apply_pass(run.states[bid], refl, read.dn["SCL"], read.items, read.item_index,
                                             day, scope, run.registry, rule=run.rule, res_m=STAGE.res)
            o, nw = res["old"], res["new"]
            valid = scope & burnscar.valid_scl(read.dn["SCL"]) & np.isfinite(refl["B12"])
            per_pass.append({"block": bid, "day": str(stac.from_day_number(day)), "used": o.used,
                             "hazy": o.hazy, "haze_b02_rise": o.stats.get("haze_b02_rise"),
                             "ddv": o.stats.get("haze_dark_target"), "deferring": nw.stats.get("deferring"),
                             "valid_px": o.stats.get("valid_px"),
                             "old_burn_px": o.stats.get("burn_px"), "new_burn_px": nw.stats.get("burn_px"),
                             "deferred_px": nw.stats.get("deferred_burn_px"), "opened_px": nw.stats.get("opened_px")})
            if not o.used:
                continue
            for k, r in res.items():
                if r.burn is not None and r.burn.any():
                    ev = {kk: np.where(r.burn, v, 0).astype(np.float32) for kk, v in r.evidence.items()}
                    if r.deferred is not None:
                        ev["deferred"] = r.deferred.astype(np.float32)
                    tags = {"pre_item": r.pre_item, "post_item": r.post_item,
                            "hazy": np.full(r.burn.shape, int(r.hazy), np.uint16)}
                    runs[k].parts[day].extend(burnscar.extract_parts(r.burn, r.key, btf, ev, tags))
            # The looser test, on the old rule's evidence and memory.
            ev = o.evidence
            gap = np.where(last_day_before > 0, day - last_day_before, 10_000)
            with np.errstate(invalid="ignore"):
                lmask = (valid & (last_day_before > 0) & (gap <= LOOSE["max_gap_days"])
                         & (ev["d_b12"] > LOOSE["d_b12"]) & (ev["d_b8a"] > LOOSE["d_b8a"])
                         & (ev["bais2_post"] >= LOOSE["bais2_min"]) & (ev["tbi_post"] <= LOOSE["tbi_max"])
                         & ((burn_day_before == 0) | (day - burn_day_before > OLD.rearm_days)))
                sdark = (valid & (last_day_before > 0) & (gap <= OLD.max_gap_days)
                         & (ev["d_b11"] > OLD.d_b11) & (ev["d_b12"] > OLD.d_b12))
            if lmask.any():
                lev = {kk: np.where(lmask, v, 0).astype(np.float32) for kk, v in ev.items()}
                loose.parts[day].extend(burnscar.extract_parts(
                    lmask, np.where(lmask, last_day_before, 0).astype(np.uint16), btf, lev,
                    {"pre_item": o.pre_item, "post_item": o.post_item,
                     "hazy": np.full(lmask.shape, int(o.hazy), np.uint16)}))
            # Window-cropped masks and strata.
            m["old_burn"][i, rs, cs] |= o.burn[brs, bcs]
            m["new_burn"][i, rs, cs] |= nw.burn[brs, bcs]
            if nw.deferred is not None:
                m["new_deferred"][i, rs, cs] |= nw.deferred[brs, bcs]
                m["new_opened"][i, rs, cs] |= nw.opened[brs, bcs]
            m["loose"][i, rs, cs] |= lmask[brs, bcs]
            sd, lm = sdark[brs, bcs], lmask[brs, bcs]
            fd, fl = first_dark[rs, cs], first_loose[rs, cs]
            new_d, new_l = sd & (fd == 0), lm & (fl == 0)
            fd[new_d] = day
            first_dark_hazy[rs, cs][new_d] = o.hazy
            fl[new_l] = day
            first_loose_hazy[rs, cs][new_l] = o.hazy
        print(f"  block {bid} done ({time.perf_counter() - t0:.0f} s)", flush=True)

    # Clusters as the job keeps them, rasterised back onto the window grid (outlines are pixel edges).
    sensing = {s.item_id: s.datetime for s in usable}
    kept = {k: clusters(run, grid, True, sensing) for k, run in runs.items()}
    loose_clusters = clusters(loose, grid, False, sensing)
    day_index = {d: i for i, d in enumerate(days)}

    def raster(cl: list[dict[str, Any]]) -> tuple[np.ndarray, list[np.ndarray]]:
        season = np.zeros(win_shape, bool)
        pix = []
        for c in cl:
            mask = features.rasterize([(c["geom_utm"], 1)], out_shape=win_shape, transform=win_tf,
                                      dtype=np.uint8).astype(bool) if c["geom_utm"].intersects(win_box) else None
            pix.append(mask)
            if mask is not None:
                season |= mask
        return season, pix

    season = {}
    cpix = {}
    for k in ("old", "new"):
        season[k], cpix[k] = raster(kept[k])
    _, loose_pix = raster(loose_clusters)
    kept_pass = {k: np.zeros((n, *win_shape), bool) for k in ("old", "new")}
    for k in ("old", "new"):
        for c, mask in zip(kept[k], cpix[k], strict=True):
            if mask is not None:
                kept_pass[k][day_index[c["post_day"]]] |= mask
    px_ha = grid.res**2 / 10_000
    sc = win_scope

    def ha(mask: np.ndarray) -> float:
        return round(float((mask & sc).sum()) * px_ha, 1)

    old_s, new_s = season["old"], season["new"]
    new_only, old_only = new_s & ~old_s, old_s & ~new_s
    deferred_kept = (m["new_deferred"] & kept_pass["new"]).any(axis=0)
    report = {
        "name": args.name, "tile": args.tile, "window_utm": [wx0, wy0, wx1, wy1], "window_px": list(win_shape),
        "blocks": [job._block_id(w) for w in blocks], "passes": [str(stac.from_day_number(d)) for d in days],
        "cropland_ha_in_window": ha(np.ones(win_shape, bool)),
        "detected_ha": {"old": ha(old_s), "new": ha(new_s)},
        "new_only_ha": ha(new_only), "old_only_ha": ha(old_only),
        "new_only_share_of_old": round(ha(new_only) / max(ha(old_s), 1e-9), 3),
        "new_only_ha_first_dark_on_hazy_pass": ha(new_only & (first_dark > 0) & first_dark_hazy),
        "new_only_ha_first_loose_on_hazy_pass": ha(new_only & (first_loose > 0) & first_loose_hazy),
        "new_only_ha_via_pending": ha(new_only & deferred_kept),
        "loose_any_ha": ha(m["loose"].any(axis=0)),
        "loose_only_ha": {"old": ha(m["loose"].any(axis=0) & ~old_s), "new": ha(m["loose"].any(axis=0) & ~new_s)},
        "loose_only_first_on_hazy_pass_ha": {
            k: ha(m["loose"].any(axis=0) & ~season[k] & (first_loose > 0) & first_loose_hazy) for k in ("old", "new")},
        "clusters_in_window": {k: sum(p is not None and bool((p & sc).any()) for p in cpix[k]) for k in ("old", "new")},
        "per_pass": per_pass,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    print(json.dumps({k: v for k, v in report.items() if k != "per_pass"}, indent=1), flush=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))

    def describe(c: dict[str, Any], pix: np.ndarray | None, rule: str) -> dict[str, Any] | None:
        if pix is None or not (pix & sc).any():
            return None
        anchor = job._anchor(c["geom_utm"])
        geom_ll = job._to_lonlat(c["geom_utm"], grid.epsg)
        return {
            "rule": rule, "post": str(stac.from_day_number(c["post_day"])), "pre": str(stac.from_day_number(c["pre_day"])),
            "pre_item": c["pre_item"], "post_item": c["post_item"], "n_px": c["n_px"],
            "area_ha": round(c["n_px"] * px_ha, 2), "hazy_post": c["hazy_post"], "deferred_px": c["deferred_px"],
            "means": c["means"], "anchor_in_window": bool(win_box.contains(anchor)),
            "in_old_share": round(float((pix & old_s).sum()) / max(int(pix.sum()), 1), 3),
            "in_new_share": round(float((pix & new_s).sum()) / max(int(pix.sum()), 1), 3),
            "first_dark_hazy_share": round(float((pix & first_dark_hazy & (first_dark > 0)).sum()) / max(int(pix.sum()), 1), 3),
            "geom_ll": mapping(geom_ll), "geom_utm_wkt": c["geom_utm"].wkt, "epsg": grid.epsg, "tile": args.tile,
        }

    cl_out = []
    for k in ("old", "new"):
        for c, pix in zip(kept[k], cpix[k], strict=True):
            d = describe(c, pix, k)
            if d:
                cl_out.append(d)
    for c, pix in zip(loose_clusters, loose_pix, strict=True):
        d = describe(c, pix, "loose")
        if d:
            cl_out.append(d)
    (out_dir / "clusters.json").write_text(json.dumps(cl_out))
    np.savez_compressed(out_dir / "masks.npz", days=np.array(days), scope=win_scope, first_dark=first_dark,
                        first_dark_hazy=first_dark_hazy, first_loose=first_loose, first_loose_hazy=first_loose_hazy,
                        kept_old=kept_pass["old"], kept_new=kept_pass["new"], **m,
                        window_utm=np.array([wx0, wy0, wx1, wy1]), epsg=np.array(grid.epsg))
    print(f"wrote {out_dir} in {time.perf_counter() - t0:.0f} s; clusters: "
          f"{ {k: sum(1 for c in cl_out if c['rule'] == k) for k in ('old', 'new', 'loose')} }", flush=True)


if __name__ == "__main__":
    main()
