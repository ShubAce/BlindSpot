"""Blind audit chips for the 40 m vs 20 m replay (scripts/burn_res_replay.py).

Draws a random sample (seeded) of the 20 m replay's kept clusters whose anchor lies in an inner
window, pooled over the replayed windows, in two strata:
  both      - at least half of the cluster's 20 m pixels are detected at 40 m too (some pass),
  20m_only  - less than half of them are detected at 40 m,
and renders each as one PNG for judges who must not know where it came from: three panels at 10 m
around the cluster (about 1.5 km): true colour (B04, B03, B02) before, true colour after, and false
colour (B12, B8A, B04) after, in the site's own colour recipes (sources/pcdata.colourise), the
cluster outlined in yellow on all three, and the dates in a small caption. Nothing else is written
on the picture, and the files are numbered chip_001.png ... in a shuffled order. The key (which
chip is which cluster and stratum) goes to a separate JSON file for whoever scores the verdicts.

"Before" is the cluster's pre pass, or the nearest earlier pass that is clear over the chip; "after"
is its post pass, or the nearest later pass (within 10 days) that is clear over the chip: clear
means under 5% cloud or cloud shadow in SCL and a dark-target haze estimate (median(B02 - 0.25 B12)
over 0.01 < B12 < 0.13) under 0.04. The same choice is made for every stratum.

Both strata are drawn with the same 20 m outline, so the outline's look says nothing about the
stratum. Adapted from scripts/burn_chips.py of commit d4c84b2 (the rules-3 audit).

Run from services/api:
  uv run python scripts/burn_res_chips.py <replay_dir> <out_chips_dir> <key_json> [--seed 20261123]
      [--both 40] [--only20 40]
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
from rasterio.enums import Resampling
from shapely import wkt
from shapely.geometry import box, mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api: the blindspot package
from blindspot import provenance

provenance.record_fetch = lambda *a, **k: None  # no source_fetch rows anywhere

from blindspot.jobs import sentinel as job
from blindspot.sources import pcdata, stac

RES = 10  # metres per chip pixel
HALF_M = 750  # at least 1.5 km across
UPSCALE = 3
CLOUDY_SCL = (3, 8, 9, 10)  # cloud shadow, cloud medium/high probability, thin cirrus
CLEAR_MAX_CLOUD = 0.05
CLEAR_MAX_HAZE = 0.04
MAX_STEP_DAYS = 10


BOTH_SHARE = 0.5  # as burn_res_replay.BOTH_SHARE


def strata(clusters: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in clusters:
        if c["res"] != 20 or not c["anchor_in_window"]:
            continue
        out["both" if c["share_other"] >= BOTH_SHARE else "20m_only"].append(c)
    return out


def chip_box(c: dict[str, Any]) -> tuple[float, float, float, float]:
    g = wkt.loads(c["geom_utm_wkt"])
    x0, y0, x1, y1 = g.bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(HALF_M, (x1 - x0) / 2 + 200, (y1 - y0) / 2 + 200)
    half = round(half / 20) * 20  # whole 20 m pixels, so 10 m and 20 m bands align
    cx, cy = round(cx / 20) * 20, round(cy / 20) * 20
    return cx - half, cy - half, cx + half, cy + half


def read_bands(items: list[stac.SceneItem], bounds: tuple[float, float, float, float],
               bands: tuple[str, ...]) -> dict[str, np.ndarray]:
    """DN over `bounds` at 10 m for one pass (its items mosaicked, least nodata first)."""
    shape = (round((bounds[3] - bounds[1]) / RES), round((bounds[2] - bounds[0]) / RES))
    items = sorted(items, key=lambda s: s.nodata_pct if s.nodata_pct is not None else 100.0)
    out: dict[str, np.ndarray] = {}
    for item in items:
        got = {}
        for b in bands:
            resampling = Resampling.nearest if b == "SCL" else Resampling.bilinear
            got[b] = stac._read_one(item.hrefs[b], bounds, shape, resampling)
        has = got["B04"] > 0 if "B04" in got else got[bands[0]] > 0
        for b, arr in got.items():
            if b not in out:
                out[b] = np.zeros(shape, arr.dtype)
            take = has & (out[b] == 0)
            out[b][take] = arr[take]
    return out


def clarity(items: list[stac.SceneItem], bounds: tuple[float, float, float, float]) -> tuple[float, float | None]:
    """(cloud or shadow fraction from SCL, dark-target haze estimate) over the chip."""
    d = read_bands(items, bounds, ("SCL", "B02", "B12"))
    data = d["SCL"] > 0
    if not data.any():
        return 1.0, None
    cloud = float(np.isin(d["SCL"], CLOUDY_SCL)[data].mean())
    first = items[0]
    b02 = stac.reflectance(d["B02"], first.scale["B02"], first.offset["B02"])
    b12 = stac.reflectance(d["B12"], first.scale["B12"], first.offset["B12"])
    with np.errstate(invalid="ignore"):
        dark = data & (b12 > 0.01) & (b12 < 0.13)
    haze = float(np.median((b02 - 0.25 * b12)[dark])) if dark.sum() >= 100 else None
    return cloud, haze


def pick_pass(days: list[dt.date], by_day: dict[dt.date, list[stac.SceneItem]], start: dt.date,
              direction: int, bounds: tuple[float, float, float, float]) -> tuple[dt.date, dict[str, Any]]:
    """The pass on `start`, or the nearest one in `direction` within MAX_STEP_DAYS that is clear over
    the chip; `start` itself if none is."""
    candidates = [d for d in days if 0 <= (d - start).days * direction <= MAX_STEP_DAYS]
    candidates.sort(key=lambda d: abs((d - start).days))
    notes: dict[str, Any] = {}
    for d in candidates:
        cloud, haze = clarity(by_day[d], bounds)
        notes[str(d)] = {"cloud": round(cloud, 3), "haze": None if haze is None else round(haze, 4)}
        if cloud < CLEAR_MAX_CLOUD and haze is not None and haze < CLEAR_MAX_HAZE:
            return d, notes
    return start, notes


def outline_px(c: dict[str, Any], bounds: tuple[float, float, float, float]) -> list[list[tuple[float, float]]]:
    g = wkt.loads(c["geom_utm_wkt"])
    rings = [g.exterior, *g.interiors] if g.geom_type == "Polygon" else [p.exterior for p in g.geoms]
    return [[((x - bounds[0]) / RES * UPSCALE, (bounds[3] - y) / RES * UPSCALE) for x, y in r.coords] for r in rings]


def render(c: dict[str, Any], by_day: dict[dt.date, list[stac.SceneItem]], days: list[dt.date],
           path: Path) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    bounds = chip_box(c)
    pre, pre_notes = pick_pass(days, by_day, dt.date.fromisoformat(c["pre"]), -1, bounds)
    post, post_notes = pick_pass(days, by_day, dt.date.fromisoformat(c["post"]), +1, bounds)
    before = read_bands(by_day[pre], bounds, ("B04", "B03", "B02"))
    after = read_bands(by_day[post], bounds, ("B04", "B03", "B02", "B12", "B8A"))
    panels = [
        pcdata.colourise("truecolor", [before["B04"], before["B03"], before["B02"]]),
        pcdata.colourise("truecolor", [after["B04"], after["B03"], after["B02"]]),
        pcdata.colourise("swir", [after["B12"], after["B8A"], after["B04"]]),
    ]
    captions = [f"before  {pre}", f"after  {post}", f"after  {post}  (SWIR false colour)"]
    size = panels[0].shape[1] * UPSCALE, panels[0].shape[0] * UPSCALE
    gap, cap_h = 8, 22
    canvas = Image.new("RGB", (3 * size[0] + 2 * gap, size[1] + cap_h), (255, 255, 255))
    font = ImageFont.load_default()
    rings = outline_px(c, bounds)
    for i, (rgba, text) in enumerate(zip(panels, captions, strict=True)):
        img = Image.fromarray(rgba[..., :3]).resize(size, Image.NEAREST)
        draw = ImageDraw.Draw(img)
        for ring in rings:
            draw.line(ring + [ring[0]], fill=(255, 255, 0), width=2)
        x = i * (size[0] + gap)
        canvas.paste(img, (x, 0))
        ImageDraw.Draw(canvas).text((x + 4, size[1] + 5), text, fill=(0, 0, 0), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, optimize=True)
    return {"before": str(pre), "after": str(post), "chip_utm": bounds, "clarity": {"pre": pre_notes, "post": post_notes}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("replay", type=Path)
    ap.add_argument("chips", type=Path)
    ap.add_argument("key", type=Path)
    ap.add_argument("--seed", type=int, default=20261123)
    ap.add_argument("--both", type=int, default=40)
    ap.add_argument("--only20", type=int, default=40)
    ap.add_argument("--start", default="2026-09-20")
    ap.add_argument("--end", default="2026-11-23")
    args = ap.parse_args()
    pooled: dict[str, list[dict[str, Any]]] = defaultdict(list)
    windows = sorted(p.parent.name for p in args.replay.glob("*/clusters.json"))
    for name in windows:
        for k, v in strata(json.loads((args.replay / name / "clusters.json").read_text())).items():
            pooled[k].extend({**c, "window": name} for c in v)
    rng = random.Random(args.seed)
    wanted = {"both": args.both, "20m_only": args.only20}
    sample = []
    for k, n in wanted.items():
        pool = sorted(pooled[k], key=lambda c: (c["window"], c["post"], c["geom_utm_wkt"]))
        picked = rng.sample(pool, min(n, len(pool)))
        sample.extend({**c, "stratum": k} for c in picked)
        print(f"{k}: {len(pool)} clusters in the windows, {len(picked)} drawn", flush=True)
    rng.shuffle(sample)
    # Every pass of each tile, for the before/after choice.
    scenes_by_tile: dict[str, dict[dt.date, list[stac.SceneItem]]] = {}
    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC)
    for tile in sorted({c["tile"] for c in sample}):
        geoms = [c["geom_ll"] for c in sample if c["tile"] == tile]
        from shapely.geometry import shape
        from shapely.ops import unary_union

        area = unary_union([shape(g) for g in geoms]).buffer(0.02)
        scenes, _ = stac.search_scenes(mapping(box(*area.bounds)), start, end)
        by_day: dict[dt.date, list[stac.SceneItem]] = defaultdict(list)
        for s in scenes:
            if s.mgrs_tile == tile and (s.cloud_pct or 0) <= job.MAX_CLOUD_PCT and (s.nodata_pct or 0) < job.MAX_NODATA_PCT:
                by_day[s.obs_date].append(s)
        scenes_by_tile[tile] = by_day

    def one(i_c: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        i, c = i_c
        name = f"chip_{i + 1:03d}.png"
        by_day = scenes_by_tile[c["tile"]]
        # A pass must also cover the chip: keep the items whose footprint holds the cluster.
        g = shape_ll(c)
        cover = {d: [s for s in items if shape_ll_item(s).contains(g)] for d, items in by_day.items()}
        cover = {d: v for d, v in cover.items() if v}
        shown = render(c, cover, sorted(cover), args.chips / name)
        return {"chip": name, "path": str((args.chips / name).resolve()), "stratum": c["stratum"],
                "window": c["window"], "tile": c["tile"], "cluster_pre": c["pre"], "cluster_post": c["post"],
                "area_ha": c["area_ha"], "n_px": c["n_px"], "share_detected_at_40m": c["share_other"],
                "means": c["means"], "geom_ll": c["geom_ll"], **shown}

    with ThreadPoolExecutor(max_workers=6) as pool:
        key = list(pool.map(one, enumerate(sample)))
    args.key.parent.mkdir(parents=True, exist_ok=True)
    args.key.write_text(json.dumps({"seed": args.seed, "windows": windows,
                                    "pool_sizes": {k: len(v) for k, v in pooled.items()}, "chips": key}, indent=1))
    print(f"wrote {len(key)} chips to {args.chips} and the key to {args.key}")


def shape_ll(c: dict[str, Any]) -> Any:
    from shapely.geometry import shape

    return shape(c["geom_ll"])


def shape_ll_item(s: stac.SceneItem) -> Any:
    from shapely.geometry import shape

    return shape(s.geometry)


if __name__ == "__main__":
    main()
