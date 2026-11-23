"""How the whole-day gap fix changes stored burn-scar confidence (read-only).

burnscar.confidence multiplied a scar's score by 0.85 when gap_days > 5, with gap_days taken from
the two passes' sensing times. Same-orbit revisits drift by about a second, so an ordinary 5-day
pair (5.000012 d) could carry the penalty meant for a missed view. The fix judges whole days
(round(gap_days) > 5). This script reads every stored cluster, works out whether its stored
confidence carries the penalty, and recomputes it with the fix from the stored columns: how many
scores change, and how many more clusters pass the live map's cut (>= 0.5) and the landing page's
unseen list (unseen and >= 0.9).

Run from services/api:  uv run python scripts/burn_gap_eval.py [database_url]
(default: the local evaluation copy of production, postgresql://...@localhost:5434/blindspot_eval)
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api: the blindspot package
from blindspot.analytics.burnscar import RULE, confidence  # noqa: E402

DEFAULT_URL = "postgresql://blindspot:blindspot@localhost:5434/blindspot_eval"
LIVE_CUT, LANDING_CUT = 0.5, 0.9


def both_scores(tbi_post: float | None, d_b12: float | None, n_px: int, res_m: int,
                hazy: bool) -> tuple[float, float]:
    """confidence() of a stored cluster (4-decimal means) without and with the gap factor."""
    args = (RULE.tbi_max if tbi_post is None else tbi_post, RULE.d_b12 if d_b12 is None else d_b12,
            n_px, RULE.min_px(res_m))
    return confidence(*args, hazy=hazy, gap_days=5), confidence(*args, hazy=hazy, gap_days=6)


def main(url: str) -> None:
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
        rows = conn.execute(
            "SELECT tbi_post, d_b12, n_px, res_m, hazy, gap_days, unseen, confidence, scope FROM burn_cluster"
        ).fetchall()
    n = len(rows)
    counts: Counter[str] = Counter()
    changed_by_scope: Counter[str] = Counter()
    by_gap: Counter[tuple[float, bool, bool]] = Counter()
    worst_residual = 0.0
    for tbi, d12, npx, res, hazy, gap, unseen, stored, scope in rows:
        without, with_pen = both_scores(tbi, d12, npx, res, hazy)
        old_pen = abs(stored - with_pen) < abs(stored - without)  # which version the stored score is
        new_pen = round(gap) > 5
        by_gap[(round(gap, 2), old_pen, new_pen)] += 1
        if old_pen == new_pen:
            new = stored
            worst_residual = max(worst_residual, abs(stored - (with_pen if old_pen else without)))
        else:
            new = with_pen if new_pen else without
        counts["old_penalised"] += old_pen
        counts["new_penalised"] += new_pen
        counts["changed"] += new != stored
        counts["live_old"] += stored >= LIVE_CUT
        counts["live_new"] += new >= LIVE_CUT
        counts["landing_old"] += bool(unseen) and stored >= LANDING_CUT
        counts["landing_new"] += bool(unseen) and new >= LANDING_CUT
        changed_by_scope[scope] += new != stored
    print(f"clusters: {n}")
    print(f"stored score carries the x0.85 gap factor: {counts['old_penalised']} "
          f"({counts['old_penalised'] / n:.1%}); with whole days: {counts['new_penalised']}")
    print(f"confidence changes: {counts['changed']} ({counts['changed'] / n:.1%}); by scope: "
          + ", ".join(f"{k} {v}" for k, v in sorted(changed_by_scope.items())))
    print(f"live map (>= {LIVE_CUT}): {counts['live_old']} -> {counts['live_new']} "
          f"(+{counts['live_new'] - counts['live_old']})")
    print(f"landing unseen list (unseen and >= {LANDING_CUT}): {counts['landing_old']} -> "
          f"{counts['landing_new']} (+{counts['landing_new'] - counts['landing_old']})")
    print(f"check: largest |stored - recomputed| among unchanged scores: {worst_residual:.4f}")
    print("stored gap_days, old penalty, new penalty -> clusters:")
    for (gap, old_pen, new_pen), c in sorted(by_gap.items()):
        print(f"  {gap:5.2f}  {'x0.85' if old_pen else '  -  '}  {'x0.85' if new_pen else '  -  '}  {c}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)
