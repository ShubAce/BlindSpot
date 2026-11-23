"""Blind audit chips for seed and grow (scripts/burn_grow_eval.py): four strata, one PNG per chip.

Strata (pooled over the replayed windows, drawn with random.Random(seed)):
  added             - pieces of candidate clusters outside the baseline season mask (kind 'added' in
                      clusters.json), anchor in an inner window;
  baseline          - baseline (rules-2) replay clusters, anchor in an inner window;
  positive_control  - stored production scars (eval database) anchored in an inner window with a FIRMS
                      detection in their window (fire_matches > 0 or unseen = false);
  negative_control  - the shape of a random added or baseline cluster, moved within its window to
                      clear cropland of the same pass pair that did not darken (median dB12 and dB8A
                      <= 0, >= 90% of pixels below the grow cuts) and is >= 2 px from every scar of
                      either rule and from every stored scar.
Each chip shows the same square (at least 1.2 km) at 10 m in four panels: true colour (B04, B03,
B02) on the outline's own pre pass and post pass (top), and SWIR false colour (B12, B8A, B04) on the
same two passes (bottom), in the site's colour recipes (sources/pcdata.colourise), the outline in
yellow on all four, and the two dates in a small caption. Nothing else is on the picture, and the
files are numbered chip_001.png ... in a shuffled order. The key (which chip is which stratum and
cluster) goes to a separate JSON file.

Run from services/api:
  uv run python scripts/burn_grow_chips.py <eval_out> <chips_dir> <key_json> [--seed 20261123]
      [--added 30 --baseline 20 --positive 10 --negative 10] [--windows W1 ...] [--cache DIR --block-cache DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from rasterio.enums import Resampling
from shapely import affinity
from shapely.geometry import box, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import burn_grow_eval as ev
import burn_res_replay as rr

from blindspot.analytics import burnscar
from blindspot.analytics.burnscar import RULE
from blindspot.jobs import sentinel as job
from blindspot.sources import pcdata, stac

RES = 10  # metres per chip pixel
HALF_M = 600  # at least 1.2 km across
UPSCALE = 3
GAP, CAP_H = 6, 20


def chip_box(g: Any) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = g.bounds
    half = max(HALF_M, (x1 - x0) / 2 + 150, (y1 - y0) / 2 + 150)
    half = round(half / 20) * 20  # whole 20 m pixels, so the 10 m and 20 m bands align
    cx, cy = round((x0 + x1) / 2 / 20) * 20, round((y0 + y1) / 2 / 20) * 20
    return cx - half, cy - half, cx + half, cy + half


def read_bands(items: list[stac.SceneItem], bounds: tuple[float, float, float, float]) -> dict[str, np.ndarray]:
    """DN over `bounds` at 10 m for one pass (its items mosaicked, least nodata first)."""
    shp = (round((bounds[3] - bounds[1]) / RES), round((bounds[2] - bounds[0]) / RES))
    out: dict[str, np.ndarray] = {}
    for item in sorted(items, key=lambda s: s.nodata_pct if s.nodata_pct is not None else 100.0):
        got = {b: stac._read_one(item.hrefs[b], bounds, shp, Resampling.bilinear) for b in ("B02", "B03", "B04", "B8A", "B12")}
        has = got["B04"] > 0
        for b, arr in got.items():
            if b not in out:
                out[b] = np.zeros(shp, arr.dtype)
            take = has & (out[b] == 0)
            out[b][take] = arr[take]
    return out


def rings_px(g: Any, bounds: tuple[float, ...]) -> list[list[tuple[float, float]]]:
    polys = [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", []))
    rings = [r for p in polys for r in (p.exterior, *p.interiors)]
    return [[((x - bounds[0]) / RES * UPSCALE, (bounds[3] - y) / RES * UPSCALE) for x, y in r.coords] for r in rings]


def render(g: Any, pre_items: list[stac.SceneItem], post_items: list[stac.SceneItem], pre: str, post: str,
           path: Path) -> tuple[float, ...]:
    from PIL import Image, ImageDraw, ImageFont

    bounds = chip_box(g)
    a, b = read_bands(pre_items, bounds), read_bands(post_items, bounds)
    panels = [pcdata.colourise("truecolor", [a["B04"], a["B03"], a["B02"]]),
              pcdata.colourise("truecolor", [b["B04"], b["B03"], b["B02"]]),
              pcdata.colourise("swir", [a["B12"], a["B8A"], a["B04"]]),
              pcdata.colourise("swir", [b["B12"], b["B8A"], b["B04"]])]
    w, h = panels[0].shape[1] * UPSCALE, panels[0].shape[0] * UPSCALE
    canvas = Image.new("RGB", (2 * w + GAP, 2 * h + GAP + CAP_H), (255, 255, 255))
    rings = rings_px(g, bounds)
    for i, rgba in enumerate(panels):
        img = Image.fromarray(rgba[..., :3]).resize((w, h), Image.NEAREST)
        draw = ImageDraw.Draw(img)
        for ring in rings:
            draw.line(ring + [ring[0]], fill=(255, 255, 0), width=2)
        canvas.paste(img, ((i % 2) * (w + GAP), (i // 2) * (h + GAP)))
    caption = f"left: {pre}   right: {post}   top: true colour   bottom: SWIR (B12, B8A, B4)"
    ImageDraw.Draw(canvas).text((4, 2 * h + GAP + 4), caption, fill=(0, 0, 0), font=ImageFont.load_default())
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, optimize=True)
    return bounds


# --- strata ----------------------------------------------------------------------------------------


def eval_clusters(out: Path, windows: list[str]) -> dict[str, list[dict[str, Any]]]:
    pooled: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in windows:
        for c in json.loads((out / name / "clusters.json").read_text()):
            if c.get("anchor_in_window"):
                pooled[{"added": "added", "base": "baseline"}.get(c["kind"], c["kind"])].append(c)
    return pooled


def positive_controls(db: str, reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    from sqlalchemy import text

    out = []
    with rr.db_session(db) as session:
        for name, r in reports.items():
            ll = stac.utm_bounds_to_lonlat(r["epsg"], tuple(r["inner_utm"]))
            rows = session.execute(text("""
                SELECT id, ST_AsText(geom) AS wkt, burn_after::date AS pre, burn_before::date AS post, area_ha,
                       res_m, fire_matches, unseen
                FROM burn_cluster
                WHERE mgrs_tile = :tile AND ST_Intersects(centroid, ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326))
                  AND (fire_matches > 0 OR unseen = false)
                ORDER BY id"""), {"tile": r["tile"], **dict(zip(("x0", "y0", "x1", "y1"), ll, strict=True))}).mappings().all()
            for row in rows:
                g = job._from_lonlat(shapely.from_wkt(row["wkt"]), r["epsg"])
                if not box(*r["inner_utm"]).contains(job._anchor(g)):
                    continue
                out.append({"kind": "positive_control", "window": name, "tile": r["tile"], "epsg": r["epsg"],
                            "pre": str(row["pre"]), "post": str(row["post"]), "area_ha": float(row["area_ha"]),
                            "geom_utm_wkt": g.wkt, "stored_id": int(row["id"]), "res": int(row["res_m"]),
                            "fire_matches": row["fire_matches"], "unseen": row["unseen"]})
    return out


def negative_control(src: dict[str, Any], r: dict[str, Any], args: argparse.Namespace, rng: random.Random,
                     scenes: dict[str, dict[int, list[stac.SceneItem]]]) -> dict[str, Any] | None:
    """Move `src`'s shape within its window to clear cropland of its pass pair that did not darken."""
    from sqlalchemy import text

    res, epsg = r["res_m"], r["epsg"]
    outer, inner = tuple(r["outer_utm"]), tuple(r["inner_utm"])
    key = "w" + "_".join(str(round(v)) for v in outer)
    by_day = scenes[r["tile"]]
    pre_d, post_d = (stac.day_number(dt.date.fromisoformat(src[k])) for k in ("pre", "post"))
    if pre_d not in by_day or post_d not in by_day:
        return None
    first = by_day[pre_d][0]
    fr = rr.frame(first, res, outer, inner)
    z = np.load(args.out / r["name"] / "arrays.npz")
    scope = z["scope"]
    reads = {}
    for d in (pre_d, post_d):
        items = [s for s in by_day[d] if shape(s.geometry).intersects(box(*stac.utm_bounds_to_lonlat(epsg, outer)))]
        read, _ = ev.read_pass(args.cache, args.block_cache, fr, key, d, items)
        f0 = {s.item_id: s for s in items}.get(read.items[0], items[0])
        refl = {b: stac.reflectance(read.dn[b], f0.scale[b], f0.offset[b]) for b in ("B8A", "B12", "B11", "B04", "B08")}
        valid = scope & burnscar.valid_scl(read.dn["SCL"])
        for arr in refl.values():
            valid &= np.isfinite(arr)
        reads[d] = (refl, valid)
    (ra, va), (rb, vb) = reads[pre_d], reads[post_d]
    with np.errstate(invalid="ignore"):
        db12, db8a = ra["B12"] - rb["B12"], ra["B8A"] - rb["B8A"]
        calm = (db12 < RULE.grow_d_b12) & (db8a < RULE.grow_d_b8a)
    with rr.db_session(args.db) as session:
        rows = session.execute(text("""SELECT ST_AsText(geom) FROM burn_cluster WHERE mgrs_tile = :tile
            AND geom && ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326)"""),
            {"tile": r["tile"], **dict(zip(("x0", "y0", "x1", "y1"), stac.utm_bounds_to_lonlat(epsg, outer), strict=True))}
        ).scalars().all()
    scars = z["season_base"] | z["season_cand"] | rr.rasterize([job._from_lonlat(shapely.from_wkt(w), epsg) for w in rows], fr)
    away = ~rr._ndimage_dilate(scars, 2)
    ok = va & vb & away
    g = shapely.from_wkt(src["geom_utm_wkt"])
    gx0, gy0, gx1, gy1 = g.bounds
    for _ in range(3000):
        x = rng.uniform(inner[0], inner[2] - (gx1 - gx0))
        y = rng.uniform(inner[1], inner[3] - (gy1 - gy0))
        dx, dy = round((x - gx0) / res) * res, round((y - gy0) / res) * res
        moved = affinity.translate(g, dx, dy)
        m = rr.rasterize([moved], fr)
        n = int(m.sum())
        if n < RULE.min_px(res) or (ok & m).sum() < 0.95 * n:
            continue
        sel = m & ok
        if (calm & sel).sum() < 0.9 * sel.sum() or np.nanmedian(db12[sel]) > 0 or np.nanmedian(db8a[sel]) > 0:
            continue
        return {"kind": "negative_control", "window": r["name"], "tile": r["tile"], "epsg": epsg, "pre": src["pre"],
                "post": src["post"], "area_ha": round(n * res * res / 10_000, 3), "geom_utm_wkt": moved.wkt,
                "shape_from": src["kind"], "median_d_b12": round(float(np.nanmedian(db12[sel])), 4),
                "median_d_b8a": round(float(np.nanmedian(db8a[sel])), 4)}
    return None


def tile_scenes(tiles: dict[str, tuple[float, ...]], start: str, end: str) -> dict[str, dict[int, list[stac.SceneItem]]]:
    out: dict[str, dict[int, list[stac.SceneItem]]] = {}
    for tile, ll in tiles.items():
        scenes, _ = stac.search_scenes(mapping(box(*ll).buffer(0.02)), dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC),
                                       dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC))
        by_day: dict[int, list[stac.SceneItem]] = defaultdict(list)
        for s in scenes:
            if s.mgrs_tile == tile and (s.cloud_pct or 0) <= job.MAX_CLOUD_PCT and (s.nodata_pct or 0) < job.MAX_NODATA_PCT:
                by_day[stac.day_number(s.datetime)].append(s)
        out[tile] = by_day
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path, help="burn_grow_eval.py output folder")
    ap.add_argument("chips", type=Path)
    ap.add_argument("key", type=Path)
    ap.add_argument("--windows", nargs="*", default=None)
    ap.add_argument("--seed", type=int, default=20261123)
    ap.add_argument("--added", type=int, default=30)
    ap.add_argument("--baseline", type=int, default=20)
    ap.add_argument("--positive", type=int, default=10)
    ap.add_argument("--negative", type=int, default=10)
    ap.add_argument("--db", default=rr.DEFAULT_DB)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--block-cache", type=Path, default=None)
    ap.add_argument("--start", default="2026-09-25")
    ap.add_argument("--end", default="2026-11-23")
    args = ap.parse_args()
    args.cache = args.cache or (args.out / "cache")
    windows = args.windows or sorted(p.parent.name for p in args.out.glob("*/clusters.json"))
    reports = {w: json.loads((args.out / w / "report.json").read_text()) for w in windows}
    rng = random.Random(args.seed)
    pooled = eval_clusters(args.out, windows)
    pooled["positive_control"] = positive_controls(args.db, reports)
    tiles: dict[str, list[float]] = {}
    for r in reports.values():
        ll = stac.utm_bounds_to_lonlat(r["epsg"], tuple(r["outer_utm"]))
        t = tiles.setdefault(r["tile"], list(ll))
        t[:] = [min(t[0], ll[0]), min(t[1], ll[1]), max(t[2], ll[2]), max(t[3], ll[3])]
    scenes = tile_scenes({k: tuple(v) for k, v in tiles.items()}, args.start, args.end)
    sample: list[dict[str, Any]] = []
    for stratum, n in (("added", args.added), ("baseline", args.baseline), ("positive_control", args.positive)):
        pool = sorted(pooled[stratum], key=lambda c: (c["window"], c["post"], c["geom_utm_wkt"]))
        picked = rng.sample(pool, min(n, len(pool)))
        sample.extend({**c, "stratum": stratum} for c in picked)
        print(f"{stratum}: {len(pool)} in the windows, {len(picked)} drawn", flush=True)
    shapes = sorted(pooled["added"] + pooled["baseline"], key=lambda c: (c["window"], c["post"], c["geom_utm_wkt"]))
    negatives, tries = [], 0
    while len(negatives) < args.negative and tries < 20 * args.negative:
        tries += 1
        src = rng.choice(shapes)
        got = negative_control(src, reports[src["window"]], args, rng, scenes)
        if got is not None:
            negatives.append({**got, "stratum": "negative_control"})
    print(f"negative_control: {len(negatives)} placed in {tries} tries", flush=True)
    sample.extend(negatives)
    rng.shuffle(sample)

    def one(i_c: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        i, c = i_c
        name = f"chip_{i + 1:03d}.png"
        g = shapely.from_wkt(c["geom_utm_wkt"])
        by_day = scenes[c["tile"]]
        cover = box(*stac.utm_bounds_to_lonlat(c["epsg"], chip_box(g)))
        days = {k: [s for s in by_day[stac.day_number(dt.date.fromisoformat(c[k]))] if shape(s.geometry).intersects(cover)]
                for k in ("pre", "post")}
        bounds = render(g, days["pre"], days["post"], c["pre"], c["post"], args.chips / name)
        return {"chip": name, "path": str((args.chips / name).resolve()), "stratum": c["stratum"],
                "window": c["window"], "tile": c["tile"], "pre": c["pre"], "post": c["post"], "area_ha": c["area_ha"],
                "chip_utm": bounds, "geom_ll": mapping(job._to_lonlat(g, c["epsg"])),
                **{k: c[k] for k in ("seed_share", "parent_area_ha", "means", "stored_id", "fire_matches", "unseen",
                                     "shape_from", "median_d_b12", "median_d_b8a", "hazy") if k in c}}

    with ThreadPoolExecutor(max_workers=6) as pool:
        key = list(pool.map(one, enumerate(sample)))
    args.key.parent.mkdir(parents=True, exist_ok=True)
    args.key.write_text(json.dumps({"seed": args.seed, "windows": windows,
                                    "pool_sizes": {k: len(v) for k, v in pooled.items()}, "chips": key}, indent=1))
    print(f"wrote {len(key)} chips to {args.chips} and the key to {args.key}")


if __name__ == "__main__":
    main()
