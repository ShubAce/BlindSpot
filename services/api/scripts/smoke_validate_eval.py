"""Re-run the smoke validation on a database without writing to it, with fixed seeds.

The evaluation behind the numbers smoke_validate reports. It loads the inputs exactly as the job
does at --run-at (jobs/smoke.py load_validation_inputs), runs the hindcast and the scores
(run_validation) with the window's own seed and with --seeds - 1 more, and prints Delhi's lag-0 r,
partial r, n_eff and best lag, the median r across cities, CAMS, ventilation alone, the placebos,
and how much each of them moves from seed to seed (the Monte Carlo noise). Nothing is written.

    cd services/api
    uv run python scripts/smoke_validate_eval.py \
        --database-url postgresql+psycopg://blindspot:blindspot@localhost:5434/blindspot_eval \
        --run-at 2026-11-22T18:25:57.001984Z --seeds 5 --out result.json

--run-at 2026-11-22T18:25:57.001984Z is the production run whose row (smoke_run 2100) the
2026-11-22 copy of the database holds; the code before these changes reproduces that row exactly.

To attribute a change, each flag puts one earlier behaviour back:
  --ceil-labels      PM2.5 rows on the hour after their period end (ceil), not on its centre
  --no-spinup        score from the first hour of the hindcast (no 48 h spin-up)
  --equal-particles  the same number of particles per source (3 to 40) instead of by tonnes
  --keep-static      release smoke from persistent industrial heat sources too
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--run-at", required=True, help="ISO time the job would have run (UTC)")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--ceil-labels", action="store_true")
    ap.add_argument("--no-spinup", action="store_true")
    ap.add_argument("--equal-particles", action="store_true")
    ap.add_argument("--keep-static", action="store_true")
    ap.add_argument("--out", type=Path)
    return ap.parse_args()


def _lag0(entry: dict[str, Any], key: str) -> dict[str, Any]:
    return ((entry.get(key) or {}).get("lag0")) or {}


def city_metrics(entry: dict[str, Any]) -> dict[str, Any]:
    model = entry["smoke_per_km_mixing_depth"]
    out = {
        "hours_observed": entry["hours_observed"],
        "r": model["lag0"].get("r"), "partial_r": model["lag0"].get("partial_r"),
        "n_eff": model["lag0"].get("n_eff"), "best_lag_h": (model.get("best_lag") or {}).get("lag_h"),
        "best_lag_r": (model.get("best_lag") or {}).get("r"),
        "r_ventilation_only": _lag0(entry, "ventilation_only").get("r"),
        "best_lag_h_ventilation_only": ((entry.get("ventilation_only") or {}).get("best_lag") or {}).get("lag_h"),
        "r_cams": _lag0(entry, "cams_baseline").get("r"),
    }
    placebos = entry.get("placebos")
    if placebos:
        const = placebos["constant_smoke"]
        out["r_placebo_constant_smoke"] = const["lag0"].get("r")
        out["best_lag_h_placebo_constant_smoke"] = (const.get("best_lag") or {}).get("lag_h")
        shift = placebos["day_shift"]
        out["r_placebo_day_shift_mean"] = shift.get("mean_r")
        out["r_placebo_day_shift_max"] = shift.get("max_r")
        out["day_shift"] = shift.get("by_shift")
    return out


def run_metrics(summary: dict[str, Any], series: list[dict[str, Any]]) -> dict[str, Any]:
    cities = {e["city"]: city_metrics(e) for e in summary["cities"]}
    return {
        "headline": summary.get("headline"),
        "median_r": summary.get("median_r_smoke_per_km_mixing_depth"),
        "median_r_cams": summary.get("median_r_cams"),
        "median_r_ventilation_only": summary.get("median_r_ventilation_only"),
        "median_r_placebo_constant_smoke": summary.get("median_r_placebo_constant_smoke"),
        "median_r_placebo_day_shift": summary.get("median_r_placebo_day_shift"),
        "placebo_comparison": summary.get("placebo_comparison"),
        "monte_carlo": summary.get("monte_carlo"),
        "inputs": summary.get("inputs"),
        "cities": cities,
        "series": {s["city"]: [p["smoke_per_km_mixing_depth"] for p in s["series"]] for s in series},
    }


def spread(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Across seeds: SD of Delhi's lag-0 r and of the median r, and of every city's lag-0 r, and
    the median over hours of the coefficient of variation of Delhi's hourly smoke."""

    def sd(values: list[float | None]) -> float | None:
        vals = [v for v in values if v is not None]
        return round(statistics.stdev(vals), 4) if len(vals) > 1 else None

    out: dict[str, Any] = {"seeds": len(runs)}
    delhi = [r["cities"].get("Delhi", {}).get("r") for r in runs]
    out["delhi_r"] = {"values": delhi, "sd": sd(delhi)}
    med = [r["median_r"] for r in runs]
    out["median_r"] = {"values": med, "sd": sd(med)}
    out["city_r_sd"] = {c: sd([r["cities"][c]["r"] for r in runs]) for c in runs[0]["cities"]}
    for key in ("median_r_placebo_constant_smoke", "median_r_placebo_day_shift"):
        if runs[0].get(key) is not None:
            out[key] = {"values": [r[key] for r in runs], "sd": sd([r[key] for r in runs])}
    if "Delhi" in runs[0]["series"]:
        stack = np.array([r["series"]["Delhi"] for r in runs], dtype=float)
        mean = stack.mean(axis=0)
        cv = stack.std(axis=0, ddof=1)[mean > 0] / mean[mean > 0]
        out["delhi_hourly_cv_median"] = round(float(np.median(cv)), 4) if cv.size else None
    return out


def main() -> int:
    args = parse_args()
    if not args.database_url:
        sys.exit("give --database-url (or set DATABASE_URL)")
    os.environ["DATABASE_URL"] = args.database_url  # before blindspot reads its settings
    os.environ["ENABLE_SCHEDULER"] = "0"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blindspot.db import session_scope
    from blindspot.jobs import smoke as job

    if args.ceil_labels:
        job.obs_hour = lambda t, period_minutes, is_model: math.ceil(t / 3600.0) * 3600.0
    if args.no_spinup:
        def first_hour_window(run_at, wind_t0, wind_end):  # t0 = the first scored hour
            now = run_at.timestamp()
            t_end = min(math.floor(now / 3600.0), math.floor(wind_end / 3600.0)) * 3600.0
            first = math.ceil(max(now - job.VALIDATION_MAX_DAYS * 86400.0, wind_t0) / 3600.0) * 3600.0
            return (first, first, t_end) if first < t_end else None
        job.validation_window = first_hour_window
    if args.equal_particles:
        build = job.build_forward_particles
        job.build_forward_particles = lambda *a, **k: build(
            *a, **{**k, "allocation": "equal", "budget": job.FWD_PARTICLES, "floor": job.PER_SOURCE_MIN})
    if args.keep_static:
        job.load_fires = functools.partial(job.load_fires, exclude_static=False)

    run_at = datetime.fromisoformat(args.run_at.replace("Z", "+00:00"))
    started = time.perf_counter()
    with session_scope() as session:
        inp = job.load_validation_inputs(session, run_at)
        session.rollback()  # read only
    if isinstance(inp, dict):
        print(json.dumps(inp, default=str))
        return 1
    load_s = time.perf_counter() - started
    base = job.validation_seed(inp.t0, inp.t_end)
    runs = []
    for k in range(args.seeds):
        t = time.perf_counter()
        summary, series = job.run_validation(inp, base + k)
        m = run_metrics(summary, series)
        m["seed"], m["runtime_s"] = base + k, round(time.perf_counter() - t, 1)
        runs.append(m)
        d = m["cities"].get("Delhi") or {}
        print(f"seed {base + k}: Delhi r {d.get('r')} partial {d.get('partial_r')} n_eff {d.get('n_eff')} "
              f"best lag {d.get('best_lag_h')} h (r {d.get('best_lag_r')}); median r {m['median_r']}; "
              f"{m['runtime_s']} s", flush=True)
    result = {
        "run_at": run_at.isoformat(),
        "flags": {k: getattr(args, k) for k in ("ceil_labels", "no_spinup", "equal_particles", "keep_static")},
        "window": {"hindcast_from": datetime.fromtimestamp(inp.t0, tz=run_at.tzinfo).isoformat(),
                   "scored_from": datetime.fromtimestamp(inp.t_score, tz=run_at.tzinfo).isoformat(),
                   "scored_to": datetime.fromtimestamp(inp.t_end, tz=run_at.tzinfo).isoformat(),
                   "scored_hours": int((inp.t_end - inp.t_score) / 3600) + 1},
        "fires_note": next((n for n in inp.notes if "FIRMS" in n), None),
        "load_s": round(load_s, 1),
        "runs": runs,
        "spread": spread(runs),
    }
    first = runs[0]
    print(json.dumps({"window": result["window"], "production_seed": {
        k: first[k] for k in ("headline", "median_r", "median_r_cams", "median_r_ventilation_only",
                              "median_r_placebo_constant_smoke", "median_r_placebo_day_shift")},
        "delhi": {k: v for k, v in (first["cities"].get("Delhi") or {}).items() if k != "day_shift"},
        "spread": {k: v for k, v in result["spread"].items() if k != "city_r_sd"}}, indent=1, default=str))
    if args.out:
        args.out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
