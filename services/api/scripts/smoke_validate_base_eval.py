"""Run another version's smoke_validate job as of a past time, on a local writable database copy.

The "before" half of scripts/smoke_validate_eval.py: it imports the job from --code-dir (the
services/api folder of a checkout of the code to evaluate, e.g. `git worktree add ../base 1d192d0`),
makes it believe the clock reads --run-at plus --offset seconds, runs smoke_validate(), reads the
row it wrote, deletes that row (and its cached response), and saves the summary and every city's
hourly series as JSON. The code of 1d192d0 drew its Monte Carlo seed from the clock, so --offset
0, 1, 2, ... give that code's seed spread on the same window.

It WRITES one smoke_run row and deletes it again, so it only runs against a database on this
machine (a restored copy, e.g. eval_smoke on localhost:5434), never production:

    cd services/api
    uv run python scripts/smoke_validate_base_eval.py --code-dir ../../base/services/api \
        --database-url postgresql+psycopg://blindspot:blindspot@localhost:5434/eval_smoke \
        --run-at 2026-11-22T18:25:57.001984Z --offset 0 --out before_seed0.json

With 1d192d0, --run-at 2026-11-22T18:25:57.001984Z and --offset 0 it reproduces production
validation row 2100 exactly (Delhi r -0.12, n_eff 9.9, median r -0.064).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code-dir", type=Path, required=True, help="services/api of the code to run")
    ap.add_argument("--database-url", required=True, help="a writable LOCAL copy")
    ap.add_argument("--run-at", required=True, help="ISO time the job would have run (UTC)")
    ap.add_argument("--offset", type=int, default=0, help="seconds added to --run-at (the old seed)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if urlsplit(args.database_url.replace("+psycopg", "")).hostname not in ("localhost", "127.0.0.1"):
        sys.exit("this script writes to the database: give a local copy (localhost), never production")

    code_dir = args.code_dir.resolve()
    os.environ["DATABASE_URL"] = args.database_url  # before blindspot reads its settings
    os.environ["ENABLE_SCHEDULER"] = "0"
    sys.path.insert(0, str(code_dir))
    from sqlalchemy import text

    from blindspot.db import session_scope
    from blindspot.jobs import smoke as job

    if not Path(job.__file__).resolve().is_relative_to(code_dir):
        sys.exit(f"imported {job.__file__}, not the code in {code_dir}")
    run_at = datetime.fromisoformat(args.run_at) + timedelta(seconds=args.offset)
    job.utcnow = lambda: run_at
    started = time.perf_counter()
    res = job.smoke_validate()
    elapsed = time.perf_counter() - started
    if "id" not in res:
        print(json.dumps(res, default=str))
        return 1
    with session_scope() as s:
        row = s.execute(text("SELECT summary, receptors, t0, hours FROM smoke_run WHERE id = :id"),
                        {"id": res["id"]}).one()
        s.execute(text("DELETE FROM response_body WHERE key = :k"), {"k": f"smoke/validation/{res['id']}"})
        s.execute(text("DELETE FROM smoke_run WHERE id = :id"), {"id": res["id"]})
    summary, receptors = row[0], row[1]
    cities = {}
    for c in summary["cities"]:
        m = c["smoke_per_km_mixing_depth"]
        cities[c["city"]] = {
            "r": m["lag0"]["r"], "partial_r": m["lag0"].get("partial_r"), "n": m["lag0"]["n"],
            "n_eff": m["lag0"].get("n_eff"), "best_lag": m["best_lag"],
            "r_vent": c["ventilation_only"]["lag0"]["r"], "r_cams": c["cams_baseline"]["lag0"]["r"],
            "hours_observed": c["hours_observed"],
        }
    # The seed the run used: newer code stores it (monte_carlo.seed, from the window); the code of
    # 1d192d0 stored none and drew it from the clock, by this formula.
    stored = (summary.get("monte_carlo") or {}).get("seed")
    out = {
        "code": job.__file__, "offset_s": args.offset,
        "seed": stored if stored is not None else int(run_at.timestamp()) % 1_000_003,
        "seed_source": "stored by the run" if stored is not None else "clock formula of 1d192d0",
        "t0": row[2].isoformat(), "hours": row[3], "period": summary["period"], "inputs": summary["inputs"],
        "headline": summary["headline"], "median_r": summary["median_r_smoke_per_km_mixing_depth"],
        "median_r_cams": summary["median_r_cams"], "median_r_vent": summary["median_r_ventilation_only"],
        "cities": cities, "runtime_s": round(elapsed, 1),
        "series": {r["city"]: [p["smoke_index"] for p in r["series"]] for r in receptors},
        "dilute": {r["city"]: [p["smoke_per_km_mixing_depth"] for p in r["series"]] for r in receptors},
        "obs": {r["city"]: [p["pm25_obs"] for p in r["series"]] for r in receptors},
        "cams": {r["city"]: [p["pm25_cams"] for p in r["series"]] for r in receptors},
        "series_t": [p["t"] for p in receptors[0]["series"]] if receptors else [],
    }
    args.out.write_text(json.dumps(out, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("seed", "t0", "hours", "headline", "median_r", "median_r_cams",
                                          "median_r_vent", "runtime_s")}, default=str))
    print("Delhi", json.dumps(cities.get("Delhi"), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
