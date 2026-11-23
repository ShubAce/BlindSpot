"""Does an unbiased contextual background help the GK-2A fire test? Prints only.

The background of a candidate pixel is the median and MAD of its clear neighbours that are not
candidates themselves. Every clear pixel 0.5 K above the 11 x 11 median of BTD is a candidate, so
on a noisy scene (BTD sigma ~1 K at a hazy midday) about 30% of the background's upper tail is cut
away: sigma comes out ~28% low and the median ~0.4 sigma low, and the "3.5 sigma" test acts at
about 2.1 sigma. At night (sigma ~0.2 K) the cut barely matters.

Variants (pre-registered 23 Nov 2026, before any was run; each changes only which neighbours make
the background, never the candidates or a threshold):
  baseline     the production test (geofire.detect_fires, no exclude mask).
  untruncated  the background leaves out only pixels more than max(0.5 K, 3 x the scene's robust
               sigma of BTD - local median) above the local median, plus absolute fires.
  sigma_only   the median (which sets the excess, so the FRP floor) as in production; sigma (for
               the k-sigma tests) from the `untruncated` neighbours.
  local_clip   like `untruncated`, with a local robust sigma (11 x 11 median of |BTD - local
               median| x 1.4826) in place of the scene's.

Evaluation set: every polar pass of the stored validation (geo_fire_validation, 27 Oct - 22 Nov
2026, read-only copy of production) with the GK-2A scan the job compared it with, counted only on
region pixels the polar sensor viewed within geofire.COVERED_MAX_SCAN_DEG (MODIS 40, VIIRS 50 deg).

Bar: a variant ships only if, pooled over all covered passes, its false-alarm rate is not higher
and its strong-fire hit rate not lower than the baseline's (point estimates), and both also hold
for the MODIS Aqua afternoon passes alone. Among variants that pass, the lowest pooled false-alarm
rate wins. Reported for all: hit, strong hit and false-alarm rates per regime with 95% intervals
from resampling passes, paired differences, and the change in counts by IST hour over the
10-minute series in --series.

The baseline run through this script's copy of the test must equal detect_fires pixel for pixel
(checked on every scan), so the variants differ from production only where intended.

    uv run python scripts/gk2a_eval_background.py --scans DIR ... --series f:/forge/data/cache/gk2a

Result (run 23 Nov 2026 on 189 passes, gk2a-ctx-3 without the static mask): no variant passes,
so the production background stays. Every variant lowers the covered false-alarm rate but also
the strong-fire hit rate. Pooled: baseline FAR 9.0% (177/1965), strong hit 70.3% (745/1059);
untruncated 4.9% (76/1561) and 64.2% (680/1059); sigma_only 5.0% (85/1698) and 66.4%
(703/1059); local_clip 7.6% (57/747) and 36.3% (384/1059).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import gk2a_eval_lib as L
import numpy as np
from scipy import ndimage

from blindspot.analytics import geofire as gf
from blindspot.analytics import passes as ps
from blindspot.jobs import geostationary as job

VARIANTS = ("baseline", "untruncated", "sigma_only", "local_clip")
GROUPS = ("all", "modis_aqua_day", "viirs_day", "viirs_night", "modis_terra")  # PassData.group
K_EXCLUDE = 3.0


def _backgrounds(btd, bt38, bt11, usable, cand, p):  # noqa: ANN001, ANN202
    shape = btd.shape
    nan = np.full(shape, np.nan)
    bg_btd, bg_btd_s, bg_38, bg_38_s, bg_11 = (nan.copy() for _ in range(5))
    todo_r, todo_c = np.nonzero(cand)
    for size in p.bg_sizes:
        if todo_r.size == 0:
            break
        medians, sigmas, count = gf._background([btd, bt38, bt11], usable, todo_r, todo_c, size)
        enough = (count >= p.bg_min_valid) & (count >= p.bg_min_fraction * (size * size - 1))
        r, c = todo_r[enough], todo_c[enough]
        bg_btd[r, c], bg_38[r, c], bg_11[r, c] = (m[enough] for m in medians)
        bg_btd_s[r, c], bg_38_s[r, c] = sigmas[0][enough], sigmas[1][enough]
        todo_r, todo_c = todo_r[~enough], todo_c[~enough]
    return bg_btd, bg_btd_s, bg_38, bg_38_s, bg_11


def detect_variant(
    a: L.Prepared, variant: str, p: gf.FireTestParams = gf.DEFAULT_PARAMS
) -> tuple[np.ndarray, np.ndarray]:
    """geofire.detect_fires (no exclude mask) with the background chosen by `variant`; returns
    the fire mask and the clear mask."""
    bt38, bt11, sza, area, back = a.bt38, a.bt11, a.sza, a.geom.area_km2, a.back
    valid = np.isfinite(bt38) & np.isfinite(bt11)
    w = gf._day_weight(sza, p)
    cloud = valid & gf.cloud_mask(bt11, w, p)
    clear = valid & ~cloud
    btd = bt38 - bt11
    absolute = clear & (bt38 > p.abs_bt38) & (btd > p.abs_btd)
    clear_median = float(np.nanmedian(btd[clear])) if clear.any() else np.nan
    filled = np.where(clear, btd, clear_median if np.isfinite(clear_median) else 0.0)
    local_median = ndimage.median_filter(filled, size=p.bg_sizes[1])
    resid = btd - local_median
    candidate = (clear & (resid > p.candidate_margin)) | absolute
    truncated = clear & ~candidate
    if variant == "baseline":
        use_median = use_sigma = truncated
    else:
        if variant == "local_clip":
            dev = np.abs(resid)
            fill = float(np.nanmedian(dev[clear])) if clear.any() else 0.0
            mad = ndimage.median_filter(np.where(clear, dev, fill), size=p.bg_sizes[1])
            threshold = np.maximum(p.candidate_margin, K_EXCLUDE * 1.4826 * mad)
        else:
            r = resid[clear]
            scene_sigma = 1.4826 * float(np.nanmedian(np.abs(r - np.nanmedian(r))))
            threshold = max(p.candidate_margin, K_EXCLUDE * scene_sigma)
        wide = clear & ~((clear & (resid > threshold)) | absolute)
        use_median, use_sigma = (truncated, wide) if variant == "sigma_only" else (wide, wide)
    bg_btd, bg_btd_s, bg_38, bg_38_s, bg_11 = _backgrounds(btd, bt38, bt11, use_median, candidate, p)
    if variant == "sigma_only":
        _, bg_btd_s, _, bg_38_s, _ = _backgrounds(btd, bt38, bt11, use_sigma, candidate, p)
    excess = btd - bg_btd
    frp = gf.frp_mw(bg_38, excess, area)
    deficit = p.max_bt11_deficit_night + (p.max_bt11_deficit_day - p.max_bt11_deficit_night) * w
    with np.errstate(invalid="ignore"):
        contextual = (
            candidate
            & np.isfinite(bg_btd)
            & np.isfinite(bg_btd_s)
            & (excess > p.k_btd * np.maximum(bg_btd_s, p.sigma_floor))
            & (bt38 - bg_38 > p.k_bt38 * np.maximum(bg_38_s, p.sigma_floor))
            & (bt11 - bg_11 > -deficit)
            & (frp >= p.min_frp_mw)
        )
    fire = absolute | contextual
    if p.cloud_buffer_px > 0:
        near_cloud = ndimage.maximum_filter(cloud.astype(np.uint8), size=2 * p.cloud_buffer_px + 1) > 0
        fire &= ~(near_cloud & (w > 0.5) & ~absolute)
    fire &= ~((sza < 90.0) & (back < p.backscatter_min_deg) & ~absolute)
    return fire, clear


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--eval-db", default=L.EVAL_DB)
    ap.add_argument("--scans", type=Path, nargs="+", required=True)
    ap.add_argument("--series", type=Path, nargs="*", default=[], help="10-minute series folders")
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    args = ap.parse_args()
    variants = [v for v in VARIANTS if v in args.variants]
    eng = L.read_only_engine(args.eval_db)
    polys = L.region_polygons(eng)
    orbits = L.orbits(eng)
    scans = L.Scans(args.scans, polys)
    passes, _ = L.stored_passes(eng, scans)
    rows: dict[str, list[dict]] = {v: [] for v in variants}
    meta = []
    for pdat in passes:
        a = scans.get(pdat.slot)
        if a is None:
            continue
        prod = a.detect().fire
        rr, cc = L.to_window(pdat, a)
        region = a.geom.region
        scan_deg = np.full(region.shape, np.nan)
        ii, jj = np.nonzero(region)
        _, scan_deg[ii, jj] = ps.pass_scan_angles(orbits[pdat.p.satellite].satrec(), pdat.p.mid,
                                                  a.geom.lat[ii, jj], a.geom.lon[ii, jj])  # fmt: skip
        with np.errstate(invalid="ignore"):
            covered = region & (scan_deg <= gf.COVERED_MAX_SCAN_DEG[pdat.p.instrument])
        for v in variants:
            fire, clear = detect_variant(a, v)
            if v == "baseline" and not np.array_equal(fire, prod):
                raise SystemExit(f"baseline copy differs from detect_fires at {pdat.slot}")
            m = gf.match_pass(fire, clear, region, rr, cc, pdat.frp, job.STRONG_FRP_MW)
            rows[v].append(m.counts(covered))
        meta.append(pdat)
        scans.forget(pdat.slot)
    print(f"{len(meta)} passes with their scan; baseline copy equals detect_fires on all of them")

    verdict = {}
    for g in GROUPS:
        idx = [i for i, d in enumerate(meta) if g == "all" or d.group == g]
        if not idx:
            continue
        print(f"\n== {g}: {len(idx)} passes (covered pixels only) ==")
        base = [rows["baseline"][i] for i in idx]
        pb = L.pooled(base)
        for v in variants:
            sel = [rows[v][i] for i in idx]
            pool, ci = L.pooled(sel), L.bootstrap(sel)
            print(f"  {v:12s} " + "; ".join(L.fmt_rate(pool, ci, k) for k in L.RATES))
            if v == "baseline":
                continue
            diff = L.paired_bootstrap(base, sel)
            print("  " + " " * 12 + " vs baseline: " + "; ".join(
                f"{k} {100 * d[0]:+.1f} pts [{100 * d[1]:+.1f}, {100 * d[2]:+.1f}]"
                for k, d in diff.items() if d))  # fmt: skip
            if g in ("all", "modis_aqua_day"):
                far_ok = (pool["false_alarm_rate"] or 0) <= (pb["false_alarm_rate"] or 0)
                strong_ok = (pool["hit_rate_strong"] or 0) >= (pb["hit_rate_strong"] or 0)
                verdict.setdefault(v, []).append((g, far_ok, strong_ok))
    print("\n== bar ==")
    passing = []
    for v, checks in verdict.items():
        ok = all(f and s for _, f, s in checks)
        print(f"  {v:12s} " + ", ".join(
            f"{g}: FAR {'ok' if f else 'ROSE'}, strong hit {'ok' if s else 'FELL'}" for g, f, s in checks)
            + f" -> {'PASSES' if ok else 'fails'}")  # fmt: skip
        if ok:
            passing.append(v)
    if passing:
        overall = {v: L.pooled(rows[v]) for v in passing}
        best = min(passing, key=lambda v: (overall[v]["false_alarm_rate"] or 0,
                                           -(overall[v]["hit_rate_strong"] or 0)))  # fmt: skip
        print(f"  chosen: {best}")
    else:
        print("  no variant passes: keep the production background")

    if args.series:
        series = L.Scans(args.series, polys)
        by_hour: dict[str, dict[int, int]] = {v: defaultdict(int) for v in variants}
        n_scans = defaultdict(int)
        for slot in sorted(series.paths):
            a = series.get(slot)
            if a is None:
                continue
            hour = L.ist(a.t_utc).hour
            n_scans[hour] += 1
            for v in variants:
                by_hour[v][hour] += int((detect_variant(a, v)[0] & a.geom.region).sum())
            series.forget(slot)
        print(f"\n== 10-minute series ({sum(n_scans.values())} scans): region fire pixels by IST hour ==")
        print("  hour scans " + " ".join(f"{v:>12s}" for v in variants))
        for h in sorted(n_scans, key=lambda x: (x - 12) % 24):
            print(f"  {h:02d}   {n_scans[h]:4d} " + " ".join(f"{by_hour[v][h]:12d}" for v in variants))
        for v in variants:
            day = sum(n for h, n in by_hour[v].items() if 15 <= h < 18)
            night = sum(n for h, n in by_hour[v].items() if h >= 19 or h < 6)
            print(f"  {v:12s} total {sum(by_hour[v].values()):6d}; 15-18 IST {day:6d}; 19-06 IST {night:5d}")


if __name__ == "__main__":
    main()
