"""Coverage-aware validation of the GK-2A fire record, and the polar-window accounting. Prints only.

1. Orbital elements: the scan angle passes.pass_scan_angles predicts for every FIRMS MODIS
   detection, turned into MODIS's along-scan pixel size, against the size FIRMS reports (`scan`).
   This checks the TLE before it is used to say where MODIS could see.
2. Replay: every polar pass of the stored validation (geo_fire_validation of a read-only copy of
   production) is compared again with the same GK-2A scan, with the production code. The raw counts
   must equal the stored ones exactly; then the same passes are counted only on region pixels the
   polar sensor viewed within COVERED_MAX_SCAN_DEG (MODIS 40, VIIRS 50 deg of scan angle), per
   regime, with 95% intervals from resampling passes (and the raw figures beside them), by scan
   angle, and with the most influential pass left out.
3. Windows: the share of GK-2A fire pixel-hours outside the polar windows over the last 7 days of
   the copy, as the API computed it (inclusive window ends, so a scan on a boundary counted in a
   window and in the gap after it) and as it computes it now (each scan by the share of its 10
   minutes outside), for pads of 0, 5, 10 and 15 minutes, windows on one clock or day by day.

    uv run python scripts/gk2a_eval_coverage.py --scans f:/forge/data/cache/gk2a DIR ...
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gk2a_eval_lib as L
import numpy as np
from sqlalchemy import text

from blindspot.analytics import geofire as gf
from blindspot.analytics import passes as ps
from blindspot.api.routers import geostationary as router
from blindspot.jobs import geostationary as job

MODIS_ALT_KM = 705.0
GROUPS = ("modis_aqua_day", "viirs_day", "viirs_night", "modis_terra", "all")


def check_tle(eng, orbits: dict) -> None:  # noqa: ANN001
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT satellite, acq_utc, lat, lon, scan FROM fire_detection "
                "WHERE instrument = 'MODIS' AND acq_utc >= '2026-10-27' ORDER BY acq_utc"
            )
        ).all()
    by_pass: dict[tuple[str, datetime], list] = defaultdict(list)
    for r in rows:
        by_pass[(r.satellite, r.acq_utc)].append(r)
    print("\n== 1. TLE check: MODIS pixel size from the predicted scan angle vs FIRMS `scan` ==")
    stats = defaultdict(list)
    for (sat, acq), det in by_pass.items():
        orbit = orbits.get(sat)
        if orbit is None:
            continue
        lat = np.array([d.lat for d in det])
        lon = np.array([d.lon for d in det])
        _, ang = ps.pass_scan_angles(orbit.satrec(), acq, lat, lon)
        pred = ps.along_scan_pixel_km(np.minimum(ang, 54.9), MODIS_ALT_KM)
        obs = np.array([d.scan for d in det], dtype=float)
        week = (acq - datetime(2026, 10, 27, tzinfo=UTC)).days // 7
        stats[(sat, week)] += list(zip(pred, obs, ang, strict=True))
    for (sat, week), v in sorted(stats.items()):
        a = np.array(v)
        err = a[:, 0] - a[:, 1]
        first = datetime(2026, 10, 27) + timedelta(days=7 * week)
        corr = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
        print(f"  {sat:5s} week {week} (from {first:%d %b}): n={len(a):4d}  median |pred-FIRMS| "
              f"{np.median(np.abs(err)):.2f} km, median error {np.median(err):+.2f} km, corr {corr:.3f}, "
              f"FIRMS scan {a[:, 1].min():.1f}-{a[:, 1].max():.1f} km, "
              f"angle {a[:, 2].min():.0f}-{a[:, 2].max():.0f} deg")  # fmt: skip


def replay(eng, scans: L.Scans, orbits: dict) -> list[dict]:  # noqa: ANN001
    passes, _ = L.stored_passes(eng, scans)
    out = []
    mismatches = missing = 0
    for pdat in passes:
        a = scans.get(pdat.slot)
        if a is None:
            missing += 1
            continue
        res = a.detect()
        rr, cc = L.to_window(pdat, a)
        m = gf.match_pass(res.fire, res.clear, a.geom.region, rr, cc, pdat.frp, job.STRONG_FRP_MW)
        raw = m.counts()
        stored = {k: pdat.stored[k] for k in raw}
        if raw != stored:
            mismatches += 1
            print(f"  MISMATCH {pdat.p.satellite} {pdat.p.start}: replay {raw} stored {stored}")
        orbit = orbits[pdat.p.satellite]
        region = a.geom.region
        scan_deg = np.full(region.shape, np.nan)
        ii, jj = np.nonzero(region)
        _, scan_deg[ii, jj] = ps.pass_scan_angles(orbit.satrec(), pdat.p.mid, a.geom.lat[ii, jj],
                                                  a.geom.lon[ii, jj])  # fmt: skip
        covered = region & (scan_deg <= gf.COVERED_MAX_SCAN_DEG[pdat.p.instrument])
        out.append({
            "satellite": pdat.p.satellite, "instrument": pdat.p.instrument, "regime": pdat.regime,
            "group": pdat.group, "start": pdat.p.start, "slot": pdat.slot,
            "raw": raw, "cov": m.counts(covered), "covered_share": covered.sum() / region.sum(),
            "bins": gf.scan_angle_counts(m, scan_deg),
        })  # fmt: skip
        scans.forget(pdat.slot)
    print(f"\n== 2. Replay of {len(passes)} stored passes: {len(out)} replayed, {missing} without a "
          f"scan, {mismatches} raw mismatches ==")  # fmt: skip
    return out


def in_group(r: dict, g: str) -> bool:
    if g == "all":
        return True
    if g == "viirs_day":
        return r["instrument"] == "VIIRS" and r["regime"] == "day"
    if g == "viirs_night":
        return r["instrument"] == "VIIRS" and r["regime"] == "night"
    if g == "modis_aqua_day":
        return r["satellite"] == "Aqua" and r["regime"] == "day"
    return r["satellite"] == "Terra"


def report(rows: list[dict]) -> None:
    for g in GROUPS:
        sel = [r for r in rows if in_group(r, g)]
        if not sel:
            continue
        raw, cov = [r["raw"] for r in sel], [r["cov"] for r in sel]
        pr, pc = L.pooled(raw), L.pooled(cov)
        cr, cc = gf.bootstrap_rates(raw), gf.bootstrap_rates(cov)
        share = np.mean([r["covered_share"] for r in sel])
        print(f"\n  {g}: {len(sel)} passes; mean share of the region covered {100 * share:.0f}%")
        for label, pool, ci in (("raw    ", pr, cr), ("covered", pc, cc)):
            print(f"    {label}: " + "; ".join(L.fmt_rate(pool, ci, k) for k in L.RATES))
        cov_rows = [r for r in sel if r["cov"]["geo_pixels"]]
        if len(cov_rows) >= 3:  # leave-one-pass-out influence on the covered false-alarm rate
            base = pc["false_alarm_rate"]
            infl = []
            for i in range(len(sel)):
                rest = L.pooled([r["cov"] for j, r in enumerate(sel) if j != i])
                if rest["false_alarm_rate"] is not None and base is not None:
                    infl.append((rest["false_alarm_rate"] - base, sel[i]))
            d, r = max(infl, key=lambda x: abs(x[0]))
            print(f"    most influential pass on covered FAR: {r['satellite']} "
                  f"{r['start']:%d %b %H:%M}Z ({r['cov']['geo_pixels']} GK-2A px): "
                  f"leaving it out moves FAR {100 * d:+.1f} points")  # fmt: skip
    print("\n  by scan angle (all passes; unconfirmed share of GK-2A pixels, hit rate of polar pixels):")
    for inst in ("MODIS", "VIIRS"):
        sel = [r for r in rows if r["instrument"] == inst]
        bins = defaultdict(lambda: defaultdict(list))
        for r in sel:
            for b in r["bins"]:
                for k in ("geo_pixels", "geo_hits", "ref_pixels", "ref_hits"):
                    bins[(b["lo"], b["hi"])][k].append(b[k])
        for (lo, hi), c in sorted(bins.items()):
            per_pass = [dict(geo_pixels=g, geo_hits=h, ref_pixels=rp, ref_hits=rh,
                             ref_pixels_strong=0, ref_hits_strong=0)
                        for g, h, rp, rh in zip(c["geo_pixels"], c["geo_hits"], c["ref_pixels"],
                                                c["ref_hits"], strict=True)]  # fmt: skip
            pool, ci = L.pooled(per_pass), gf.bootstrap_rates(per_pass)
            print(f"    {inst} {lo:2.0f}-{hi:2.0f} deg: " + "; ".join(
                L.fmt_rate(pool, ci, k) for k in ("false_alarm_rate", "hit_rate")))  # fmt: skip


def windows_report(eng, now: datetime, days: int = 7) -> None:  # noqa: ANN001
    since = now - timedelta(days=days)
    with eng.connect() as conn:
        slots = conn.execute(
            text(
                "SELECT t_utc, count FROM geo_fire_slot WHERE algorithm = 'gk2a-ctx-2' "
                "AND status = 'ok' AND slot_utc >= :since AND slot_utc <= :now ORDER BY slot_utc"
            ),
            {"since": since, "now": now},
        ).all()
    from sqlalchemy.orm import Session

    with Session(eng) as session:
        passes = router._polar_passes(session, since, now)
    ok = [(s.t_utc, s.count) for s in slots if s.count is not None]
    total = sum(c for _, c in ok)
    print(f"\n== 3. Polar windows, {days} days to {now:%Y-%m-%d %H:%M}Z: {len(ok)} scans, "
          f"{total * 10 / 60:.1f} pixel-hours, {len(passes)} passes ==")  # fmt: skip
    for pad in (0, 5, 10, 15):
        windows = gf.polar_windows(passes, pad)
        # as the API computed it before: inclusive ends, gaps [lo, hi) from each window's end
        old_out = sum(c for t, c in ok
                      if not any(w["start_min"] <= gf.ist_minute_of_day(t) <= w["end_min"]
                                 for w in windows))  # fmt: skip
        old_gaps = 0
        for g in router._gaps(windows):
            lo = int(g["start"][:2]) * 60 + int(g["start"][3:])
            hi = lo + g["minutes"]
            old_gaps += sum(c for t, c in ok if lo <= gf.ist_minute_of_day(t) < hi
                            or lo <= gf.ist_minute_of_day(t) + 1440 < hi)  # fmt: skip
        spans = gf.window_spans(windows)
        new_out = sum(c * (1 - gf.scan_overlap(gf.ist_minutes(t), spans)) for t, c in ok)
        new_gaps = sum(c * gf.scan_overlap(gf.ist_minutes(t), [(lo, hi)])
                       for g in router._gaps(windows)
                       for lo, hi in [(int(g["start"][:2]) * 60 + int(g["start"][3:]),
                                       int(g["start"][:2]) * 60 + int(g["start"][3:]) + g["minutes"])]
                       for t, c in ok)  # fmt: skip
        print(f"  pad {pad:2d} min: before: outside {old_out * 10 / 60:7.1f} px-h "
              f"({100 * old_out / total:.1f}%), gaps sum {old_gaps * 10 / 60:7.1f} | now: outside "
              f"{new_out * 10 / 60:7.1f} px-h ({100 * new_out / total:.1f}%), "
              f"gaps sum {new_gaps * 10 / 60:7.1f}")  # fmt: skip
    for row in gf.outside_shares(ok, passes, (0, 5, 10, 15, 30)):
        print(f"  outside share, pad {row['pad_min']:2d} min: one clock {100 * row['union']:.1f}%, "
              f"day by day {100 * row['per_day']:.1f}%")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--eval-db", default=L.EVAL_DB)
    ap.add_argument("--scans", type=Path, nargs="+", required=True)
    ap.add_argument("--now", default="2026-11-22T17:50:00+00:00", help="end of the window check")
    args = ap.parse_args()
    eng = L.read_only_engine(args.eval_db)
    orbits = L.orbits(eng)
    print("orbital elements:", ", ".join(f"{k} {v.epoch:%d %b %H:%M}" for k, v in orbits.items()))
    check_tle(eng, orbits)
    scans = L.Scans(args.scans, L.region_polygons(eng))
    rows = replay(eng, scans, orbits)
    report(rows)
    windows_report(eng, datetime.fromisoformat(args.now))


if __name__ == "__main__":
    main()
