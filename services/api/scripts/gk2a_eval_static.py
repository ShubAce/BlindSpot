"""Evaluate masks of persistent (non-crop) heat sources for the GK-2A record. Prints only.

Replays the stored GK-2A record (geo_fire_pixel, every pixel since 27 Oct) of a read-only copy of
production. For each night N (the evening it starts on, IST, as fires_stats.night_of), a mask is
built ONLY from data of earlier nights and applied to night N's detections: no look-ahead.

Mask rules (pre-registered, 23 Nov 2026):
  firms       GK-2A pixels whose footprint comes within 400 m of a recurring FIRMS night detection:
              fires_stats.find_static_sites (the rule behind fire_static_site and the FIRMS
              counts) run on FIRMS night detections of nights before N.
  firms_seen  `firms`, only where GK-2A itself saw heat on at least one deep night (23:00-05:00
              IST) of the 14 nights before N, so a kiln GK-2A cannot see does not mask the crop
              fires that share its ~12 km2 pixel.
  deep2       GK-2A detections between 23:00 and 05:00 IST on >= 2 of the 14 nights before N.
  night3      GK-2A detections at night (scan's region solar zenith >= 90 deg) on >= 3 of the 14
              nights before N (reported for comparison; evening crop fires can recur too).
The job will use the first of these unions that passes the bar: firms|deep2, firms_seen|deep2,
deep2.

Bar: the mask removes <= 1% of GK-2A detections made 16:00-18:30 IST (all nights pooled).
Also reported: share of night (solar zenith >= 90) and deep-night detections removed, of
17:00-21:00 IST detections (where the evening index lives), pixel-hours removed by IST hour, and
the mask's precision against FIRMS over the whole season: a flagged pixel counts as backed when a
production fire_static_site (computed 22 Nov from every night detection) touches its footprint,
or FIRMS made >= 3 night detections within 2 km of its centre on >= 2 nights.

With --scans, the 10-minute series in those folders is re-run with and without the mask (production
detect_fires), to show the mask's effect on the pixels around flagged ones.

    uv run python scripts/gk2a_eval_static.py --scans f:/forge/data/cache/gk2a
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import gk2a_eval_lib as L
import numpy as np
import pandas as pd
import pyproj
import shapely
from scipy import ndimage
from sqlalchemy import text

from blindspot.analytics import fires_stats as fs
from blindspot.analytics import passes as ps

LOOKBACK_NIGHTS = 14
DEEP_START, DEEP_END = 23 * 60, 5 * 60  # IST minutes: 23:00-05:00
RULES = ("firms", "firms_seen", "deep2", "night3")
UNIONS = {"firms|deep2": ("firms", "deep2"), "firms_seen|deep2": ("firms_seen", "deep2"),
          "deep2": ("deep2",), "firms|deep2|night3": ("firms", "deep2", "night3")}  # fmt: skip
BAR = 0.01


def load_history(eng) -> pd.DataFrame:  # noqa: ANN001
    with eng.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT p.slot_utc, p.t_utc, p.row, p.col, p.lon, p.lat, p.frp_mw, s.sza_deg "
                "FROM geo_fire_pixel p JOIN geo_fire_slot s USING (slot_utc, algorithm) "
                "WHERE p.algorithm = 'gk2a-ctx-2' ORDER BY p.t_utc"
            ),
            conn,
        )
    t = pd.to_datetime(df.t_utc, utc=True)
    local = t + pd.Timedelta(hours=5, minutes=30)
    df["ist_min"] = local.dt.hour * 60 + local.dt.minute
    df["night"] = (local - pd.Timedelta(hours=12)).dt.date
    df["deep"] = (df.ist_min >= DEEP_START) | (df.ist_min < DEEP_END)
    df["is_night"] = df.sza_deg >= 90
    return df


def firms_night(eng) -> pd.DataFrame:  # noqa: ANN001
    with eng.connect() as conn:
        df = pd.read_sql(
            text("SELECT lat, lon, acq_utc FROM fire_detection WHERE daynight = 'N'"), conn
        )
    df["night"] = [fs.night_of(t.to_pydatetime()) for t in pd.to_datetime(df.acq_utc, utc=True)]
    return df


class Grid:
    """GK-2A footprints of the job's window, in a local metric projection, for distance tests."""

    def __init__(self, a: L.Prepared) -> None:
        g, w = a.geom, a.scan.window
        self.nav, self.window = a.scan.navigation, w
        self.region = g.region
        self.lon, self.lat = g.lon, g.lat
        self.tr = pyproj.Transformer.from_crs(
            "EPSG:4326", "+proj=laea +lat_0=30.5 +lon_0=75.8 +units=m", always_xy=True
        )
        cx, cy = self.tr.transform(g.corner_lon, g.corner_lat)
        self.cx, self.cy = cx, cy

    def footprint_m(self, i: int, j: int) -> shapely.Polygon:
        cx, cy = self.cx, self.cy
        corners = [(i, j), (i, j + 1), (i + 1, j + 1), (i + 1, j)]
        return shapely.Polygon([(cx[a, b], cy[a, b]) for a, b in corners])

    def pixels_near(self, lat: np.ndarray, lon: np.ndarray, radius_m: float) -> set[tuple[int, int]]:
        """Full-disk (row, col) of every pixel whose footprint comes within radius_m of a point."""
        out: set[tuple[int, int]] = set()
        if len(lat) == 0:
            return out
        rr, cc = self.nav.to_rowcol(np.asarray(lon), np.asarray(lat))
        xs, ys = self.tr.transform(np.asarray(lon), np.asarray(lat))
        w = self.window
        for r, c, x, y in zip(rr, cc, xs, ys, strict=True):
            pt = shapely.Point(x, y)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    i, j = r - w.row0 + di, c - w.col0 + dj
                    if 0 <= i < w.shape[0] and 0 <= j < w.shape[1]:
                        if self.footprint_m(i, j).distance(pt) <= radius_m:
                            out.add((r + di, c + dj))
        return out


def masks_by_night(hist: pd.DataFrame, firms: pd.DataFrame, grid: Grid, nights: list[date]) -> dict:
    """{night: {rule: set of (row, col)}} using data of earlier nights only."""
    out: dict[date, dict[str, set[tuple[int, int]]]] = {}
    pix_deep = hist[hist.deep].groupby(["row", "col"]).night.apply(set)
    pix_night = hist[hist.is_night].groupby(["row", "col"]).night.apply(set)
    for n in nights:
        lo = n - timedelta(days=LOOKBACK_NIGHTS)
        deep_n = {rc: {x for x in s if lo <= x < n} for rc, s in pix_deep.items()}
        night_n = {rc: {x for x in s if lo <= x < n} for rc, s in pix_night.items()}
        past = firms[firms.night < n]
        sites = fs.find_static_sites(list(zip(past.lat, past.lon, past.night, strict=True)))
        members = [m for s in sites for m in s.members]
        firms_px = grid.pixels_near(np.array([m[0] for m in members]), np.array([m[1] for m in members]),
                                    fs.STATIC_RADIUS_KM * 1000)  # fmt: skip
        seen = {rc for rc, s in deep_n.items() if len(s) >= 1}
        out[n] = {
            "firms": firms_px,
            "firms_seen": firms_px & seen,
            "deep2": {rc for rc, s in deep_n.items() if len(s) >= 2},
            "night3": {rc for rc, s in night_n.items() if len(s) >= 3},
        }
    return out


def precision(flagged: set[tuple[int, int]], grid: Grid, eng, firms: pd.DataFrame) -> dict:  # noqa: ANN001
    """Share of flagged pixels backed by FIRMS (production static site, or >= 3 night detections
    within 2 km on >= 2 nights), over the whole season."""
    if not flagged:
        return {"n": 0}
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT ST_AsBinary(area) AS wkb FROM fire_static_site")).all()
    site_px: set[tuple[int, int]] = set()
    for r in rows:
        geom = shapely.from_wkb(bytes(r.wkb))
        pts = np.array([c for poly in shapely.get_parts(geom) for c in poly.exterior.coords])
        site_px |= grid.pixels_near(pts[:, 1], pts[:, 0], 0.0)
    w = grid.window
    backed_site = backed_firms = 0
    detail = []
    for r, c in sorted(flagged):
        i, j = r - w.row0, c - w.col0
        d = ps.ground_km(grid.lat[i, j], grid.lon[i, j], firms.lat.to_numpy(), firms.lon.to_numpy())
        near = firms[d <= 2.0]
        f_ok = len(near) >= 3 and near.night.nunique() >= 2
        s_ok = (r, c) in site_px
        backed_site += s_ok
        backed_firms += f_ok
        detail.append((int(r), int(c), round(float(grid.lat[i, j]), 3),
                       round(float(grid.lon[i, j]), 3), s_ok, len(near), near.night.nunique()))  # fmt: skip
    either = sum(1 for x in detail if x[4] or (x[5] >= 3 and x[6] >= 2))
    return {"n": len(flagged), "backed": either, "precision": round(either / len(flagged), 3),
            "static_site": backed_site, "firms_3_nights2": backed_firms, "detail": detail}  # fmt: skip


def report(hist: pd.DataFrame, masks: dict, name: str, rules: tuple[str, ...]) -> dict:
    flagged_night = [
        set().union(*(masks[n][r] for r in rules)) if n in masks else set() for n in hist.night
    ]
    removed = np.array(
        [(r, c) in f for r, c, f in zip(hist.row, hist.col, flagged_night, strict=True)]
    )
    h = hist.assign(removed=removed)
    m = h.ist_min

    def share(sel: pd.Series) -> tuple[float, int, int]:
        k, n = int(h.removed[sel].sum()), int(sel.sum())
        return (k / n if n else float("nan")), k, n

    out = {
        "night (sza>=90)": share(h.is_night),
        "after dusk 19-06": share((m >= 19 * 60) | (m < 6 * 60)),
        "deep night 23-05": share(h.deep),
        "16:00-18:30": share((m >= 16 * 60) & (m < 18 * 60 + 30)),
        "17:00-21:00": share((m >= 17 * 60) & (m < 21 * 60)),
        "day (sza<90)": share(~h.is_night),
        "all": share(m >= 0),
    }
    print(f"\n== mask {name} ==")
    for k, (s, a, n) in out.items():
        print(f"  removed {k:18s} {100 * s:6.2f}%  ({a}/{n})")
    flagged_any = set().union(*(set().union(*(masks[n][r] for r in rules)) for n in masks))
    print(f"  pixels ever flagged: {len(flagged_any)}; flagged on the last night: "
          f"{len(set().union(*(masks[max(masks)][r] for r in rules)))}")  # fmt: skip
    by_hour = h.groupby(h.ist_min // 60).agg(n=("removed", "size"), removed=("removed", "sum"))
    by_hour["pixel_hours_removed"] = (by_hour.removed * 10 / 60).round(1)
    by_hour["share"] = (by_hour.removed / by_hour.n).round(4)
    print("  by IST hour:", ", ".join(f"{hr:02d}h {r.removed}/{r.n}" for hr, r in by_hour.iterrows()))
    out["flagged_any"] = flagged_any
    return out


def ring_effect(scans_dirs: list[Path], polys, masks: dict, rules: tuple[str, ...]) -> None:  # noqa: ANN001
    scans = L.Scans(scans_dirs, polys)
    base_n = new_n = ring_base = ring_new = excl = 0
    for slot in sorted(scans.paths):
        a = scans.get(slot)
        if a is None:
            continue
        n = L.night_of(a.t_utc)
        if n not in masks:
            continue
        w = a.scan.window
        ex = np.zeros(a.geom.region.shape, dtype=bool)
        for r, c in set().union(*(masks[n][x] for x in rules)):
            if w.row0 <= r < w.row1 and w.col0 <= c < w.col1:
                ex[r - w.row0, c - w.col0] = True
        if not ex.any():
            continue
        reg = a.geom.region
        ring = (ndimage.maximum_filter(ex.astype(np.uint8), 3) > 0) & ~ex & reg
        base = a.detect()
        new = a.detect(exclude=ex)
        base_n += int((base.fire & reg).sum())
        new_n += int((new.fire & reg).sum())
        ring_base += int((base.fire & ring).sum())
        ring_new += int((new.fire & ring).sum())
        excl += int((new.excluded_fire & reg).sum())
        scans.forget(slot)
    print(f"\n== 10-minute series, re-run with the mask ({'|'.join(rules)}) ==")
    print(f"  region fire pixels: no mask {base_n}, with mask {new_n} (+{excl} on flagged pixels, "
          f"stored apart)")  # fmt: skip
    print(f"  fire pixels in the 8-neighbour ring of flagged pixels: no mask {ring_base}, "
          f"with mask {ring_new}")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--eval-db", default=L.EVAL_DB)
    ap.add_argument("--scans", type=Path, nargs="*", default=[], help="folders of .npz scans")
    ap.add_argument("--geometry-scan", type=Path, help="any one scan .npz (default: first in --scans)")
    args = ap.parse_args()
    eng = L.read_only_engine(args.eval_db)
    polys = L.region_polygons(eng)
    paths = L.scan_paths(args.scans)
    if args.geometry_scan:
        a0 = L.prepare(L.load_npz(args.geometry_scan), polys)
    else:
        a0 = L.prepare(L.load_npz(next(iter(paths.values()))), polys)
    grid = Grid(a0)
    hist = load_history(eng)
    firms = firms_night(eng)
    nights = sorted(set(hist.night))
    print(f"GK-2A record: {len(hist)} fire pixels, nights {nights[0]} - {nights[-1]}; "
          f"FIRMS night detections: {len(firms)}")  # fmt: skip
    masks = masks_by_night(hist, firms, grid, nights)
    results = {}
    for rule in RULES:
        results[rule] = report(hist, masks, rule, (rule,))
    for name, rules in UNIONS.items():
        results[name] = report(hist, masks, name, rules)
    chosen = next((n for n in ("firms|deep2", "firms_seen|deep2", "deep2")
                   if results[n]["16:00-18:30"][0] <= BAR), None)  # fmt: skip
    print(f"\nBAR (<= {100 * BAR:.0f}% of 16:00-18:30 IST removed): chosen union = {chosen}")
    for name in ("firms", "firms_seen", "deep2", "night3", *( [chosen] if chosen else [])):
        p = precision(results[name]["flagged_any"], grid, eng, firms)
        print(f"\nprecision vs FIRMS, {name}: {p.get('backed')}/{p['n']} backed "
              f"({p.get('precision')}); static site {p.get('static_site')}, "
              f">=3 FIRMS night detections within 2 km on >=2 nights {p.get('firms_3_nights2')}")  # fmt: skip
        for d in p.get("detail", []):
            print("   ", d)
    if chosen and args.scans:
        ring_effect(args.scans, polys, masks, UNIONS[chosen])


if __name__ == "__main__":
    main()
