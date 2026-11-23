"""How much of a validation window's fire emissions sit at persistent industrial heat sources.

Read-only and deterministic (no Monte Carlo): the smoke validation's own window and wind-grid box
(load_members, validation_window), then the job's load_fires with and without the fire_static_site
rule. Prints the detections, FRP and dry matter (the emitted tonnes, linear in FRP) of each set
and the static sites' share of them.

    cd services/api && uv run python scripts/smoke_static_share.py \\
        --database-url postgresql+psycopg://blindspot:blindspot@localhost:5434/blindspot_eval \\
        --run-at 2026-11-22T18:25:57.001984Z
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--run-at", required=True, help="ISO time the job would have run (UTC)")
    args = ap.parse_args()
    if not args.database_url:
        sys.exit("give --database-url (or set DATABASE_URL)")
    os.environ["DATABASE_URL"] = args.database_url  # before blindspot reads its settings
    os.environ["ENABLE_SCHEDULER"] = "0"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blindspot.db import session_scope
    from blindspot.jobs import smoke as job

    run_at = datetime.fromisoformat(args.run_at)
    with session_scope() as session:
        members, _ = job.load_members(session, run_at)
        if not members:
            sys.exit("no wind grid stored")
        window = job.validation_window(run_at, max(m.wind.t0 for m in members),
                                       min(m.wind.t_end for m in members))
        if window is None:
            sys.exit("no validation window")
        t0, _, t_end = window
        bbox = members[0].wind.bbox
        crop, note = job.load_fires(session, job._utc(t0), job._utc(t_end), bbox)
        every, _ = job.load_fires(session, job._utc(t0), job._utc(t_end), bbox, exclude_static=False)
        session.rollback()  # read only
    if crop is None or every is None:
        sys.exit(note)
    static = ~np.isin(every.id, crop.id)

    def share(values: np.ndarray) -> dict[str, float]:
        return {"static_sites": round(float(values[static].sum()), 1), "all": round(float(values.sum()), 1),
                "share": round(float(values[static].sum() / values.sum()), 4)}

    print(json.dumps({
        "window": [job._utc(t0).isoformat(), job._utc(t_end).isoformat()], "bbox": bbox, "note": note,
        "detections": {"static_sites": int(static.sum()), "all": int(every.id.size),
                       "share": round(float(static.mean()), 4)},
        "frp_mw": share(every.frp), "dry_matter_t": share(every.dm),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
