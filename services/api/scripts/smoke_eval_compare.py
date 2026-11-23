"""Before/after table for the smoke validation, from the JSON files of the two evaluation scripts.

    uv run python scripts/smoke_eval_compare.py \
        --before before_seed0.json before_seed1.json ... \
        --after final_after.json \
        --variant "no spin-up (same hours)=final_spin0.json" --variant ... \
        --static-with attr_static.json --static-without after.json

--before: outputs of scripts/smoke_validate_base_eval.py (the old code, one file per seed).
--after and --variant: outputs of scripts/smoke_validate_eval.py (several seeds per file).
For the old code it also recomputes the lag-0 r on --after's scored hours only, since the two
versions score different hours when the stored wind is too short for the full week plus spin-up.
--static-with/--static-without: two eval files with the same seeds and particle budget, with and
without persistent industrial heat sources; prints the share of the modelled smoke (smoke per km
of mixing depth, summed over the scored hours, averaged over seeds) that comes from those sources.
Means and SDs are over seeds (sample SD); r is Pearson.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blindspot.analytics.trajectory import lagged_correlations


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _mean_sd(values: list[float | None]) -> str:
    v = [x for x in values if x is not None]
    if not v:
        return "-"
    sd = f" sd {statistics.stdev(v):.4f}" if len(v) > 1 else ""
    return f"{statistics.mean(v):.3f}{sd}"


def _r(x: list[float | None], y: list[float | None]) -> float | None:
    """Lag-0 Pearson r by the validation's own rule (at least 12 hours, neither series flat)."""
    return lagged_correlations(np.array(x, dtype=float), np.array(y, dtype=float), max_lag=0)["lag0"]["r"]


def before_rows(paths: list[Path], hours: tuple[datetime, datetime] | None) -> None:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    d = [r["cities"]["Delhi"] for r in runs]
    first = runs[0]
    print(f"BEFORE ({len(runs)} seeds), scored {first['period']['from']} to {first['period']['to']}")
    print(f"  window seed {first['seed']}: Delhi r {d[0]['r']} partial {d[0]['partial_r']} n_eff {d[0]['n_eff']} "
          f"best lag {d[0]['best_lag']['lag_h']} h (r {d[0]['best_lag']['r']}) hours {d[0]['hours_observed']} | "
          f"median r {first['median_r']} CAMS {first['median_r_cams']} ventilation {first['median_r_vent']}")
    print(f"  over seeds: Delhi r {_mean_sd([x['r'] for x in d])}; median r {_mean_sd([r['median_r'] for r in runs])}; "
          f"Delhi partial {_mean_sd([x['partial_r'] for x in d])}; best lags {[x['best_lag']['lag_h'] for x in d]}")
    if hours:
        lo, hi = hours
        delhi, med = [], []
        for r in runs:
            idx = [i for i, t in enumerate(r["series_t"]) if lo <= _t(t) <= hi]
            rs = {c: _r([r["dilute"][c][i] for i in idx], [r["obs"][c][i] for i in idx]) for c in r["dilute"]}
            delhi.append(rs.get("Delhi"))
            med.append(float(np.median([v for v in rs.values() if v is not None])))
        print(f"  on the after's scored hours ({lo:%d %b %H:%M} to {hi:%d %b %H:%M}): Delhi r {_mean_sd(delhi)}; "
              f"median r {_mean_sd(med)}")


def eval_rows(name: str, path: Path) -> dict[str, Any]:
    res = json.loads(path.read_text(encoding="utf-8"))
    runs = res["runs"]
    d = [r["cities"].get("Delhi") or {} for r in runs]
    f, d0, w = runs[0], d[0], res["window"]
    sp = res["spread"]
    print(f"\n{name}: {path.name} flags {({k: v for k, v in res['flags'].items() if v})} "
          f"hindcast from {w['hindcast_from'][:16]}, scored {w['scored_from'][:16]} to {w['scored_to'][:16]} "
          f"({w['scored_hours']} h); {f['monte_carlo']['particles_per_member']} particles per member")
    print(f"  window seed {f['seed']}: Delhi r {d0.get('r')} partial {d0.get('partial_r')} n_eff {d0.get('n_eff')} "
          f"best lag {d0.get('best_lag_h')} h (r {d0.get('best_lag_r')}) hours {d0.get('hours_observed')} | "
          f"median r {f['median_r']} CAMS {f['median_r_cams']} ventilation {f['median_r_ventilation_only']} "
          f"(Delhi {d0.get('r_ventilation_only')}, best lag {d0.get('best_lag_h_ventilation_only')} h)")
    if f.get("median_r_placebo_constant_smoke") is not None:
        print(f"  placebos: 1/BLH alone median {f['median_r_placebo_constant_smoke']} (Delhi "
              f"{d0.get('r_placebo_constant_smoke')}, best lag {d0.get('best_lag_h_placebo_constant_smoke')} h); "
              f"day shift median {f['median_r_placebo_day_shift']} (Delhi mean {d0.get('r_placebo_day_shift_mean')}, "
              f"max {d0.get('r_placebo_day_shift_max')})")
    print(f"  over {len(runs)} seeds: Delhi r {_mean_sd([x.get('r') for x in d])}; median r "
          f"{_mean_sd([r['median_r'] for r in runs])}; Delhi partial {_mean_sd([x.get('partial_r') for x in d])}; "
          f"n_eff {[x.get('n_eff') for x in d]}; best lags {[x.get('best_lag_h') for x in d]}; "
          f"largest city SD {max((v for v in sp['city_r_sd'].values() if v is not None), default=None)}; "
          f"median hourly CV of Delhi's smoke {sp.get('delhi_hourly_cv_median')}")
    return res


def static_share(with_path: Path, without_path: Path) -> None:
    w = json.loads(with_path.read_text(encoding="utf-8"))["runs"]
    wo = json.loads(without_path.read_text(encoding="utf-8"))["runs"]
    shares = {}
    for city in w[0]["series"]:
        if city not in wo[0]["series"]:
            continue
        s_with = statistics.mean(float(np.nansum(np.array(r["series"][city], dtype=float))) for r in w)
        s_without = statistics.mean(float(np.nansum(np.array(r["series"][city], dtype=float))) for r in wo)
        if s_with > 0:
            shares[city] = 1.0 - s_without / s_with
    vals = sorted(shares.values())
    print(f"\nSTATIC SOURCES: share of modelled smoke from persistent industrial heat sources "
          f"({len(w)} vs {len(wo)} seeds): Delhi {shares.get('Delhi', float('nan')):.3f}; median over "
          f"{len(vals)} cities {float(np.median(vals)):.3f} (range {vals[0]:.3f} to {vals[-1]:.3f})")
    print("  " + ", ".join(f"{c} {v:.3f}" for c, v in sorted(shares.items(), key=lambda kv: -kv[1])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", type=Path, nargs="*", default=[])
    ap.add_argument("--after", type=Path)
    ap.add_argument("--variant", action="append", default=[], help="NAME=PATH")
    ap.add_argument("--static-with", type=Path)
    ap.add_argument("--static-without", type=Path)
    args = ap.parse_args()
    after = eval_rows("AFTER", args.after) if args.after else None
    hours = (_t(after["window"]["scored_from"]), _t(after["window"]["scored_to"])) if after else None
    if args.before:
        print()
        before_rows(args.before, hours)
    for v in args.variant:
        name, _, path = v.partition("=")
        eval_rows(name, Path(path))
    if args.static_with and args.static_without:
        static_share(args.static_with, args.static_without)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
