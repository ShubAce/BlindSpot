"""Evaluate the burn-risk backtest pooled over pass pairs, on a copy of a production snapshot.

Run it on a writable COPY of the database, never on the live one (it builds the backtest's temporary
tables for the whole season, minutes of CPU):

    cd services/api
    DATABASE_URL=postgresql+psycopg://blindspot:blindspot@localhost:5434/eval_risk \
    CACHE_DIR=<a folder holding risk/firms_VIIRS_SNPP_SP_2025-09-15_2025-11-30.csv> \
    uv run python scripts/risk_pooled_eval.py --out <folder>

1. Builds the backtest exactly as the `risk` job does (blindspot/jobs/risk.py backtest: every pass
   pair, its fields, their features as of t0, the risk-v3 score) and saves every field check with
   its pair, label, position, features and the two placebos to <out>/field_checks.parquet (so the
   analyses below can be rerun with --reuse without the joins).
2. Headline stability (item 3): for every day of the season, the headline the job would have shown
   that day with the data up to then, the old way (the latest pass pair alone) and the new way (every
   pair whose second pass falls in the 7 days up to the newest, pooled, shown with at least 100
   burnt and 100 unburnt field checks), with the new one's 95% interval from 30 km spatial blocks.
3. Which order to serve (item 4): pooled AUC per ISO week (of t1) of the served composite as the
   backtest can test it, of "scar hectares nearby" alone and of the composite without timing,
   forward-chained (every week after the first is held out: nothing is fitted, so each week is
   scored with what was known at its t0 only), pooled over the held-out weeks, with paired
   block-bootstrap intervals of (simple - composite), and the pre-registered rule applied.

Every figure is from this snapshot's backtest, which keeps its known biases (survivorship of the
unburnt group, different observation rules for the two labels; see docs and the job's docstring).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics import risk as model
from blindspot.db import engine
from blindspot.jobs import risk as job

SCORES = {  # name -> column of field_checks.parquet
    "score": "s",  # risk-v3 as the backtest can test it (timing and rain cannot be rebuilt: left out)
    "burned_ha_7d_only": "ha",
    "neighbourhood_only": "events",
    "fires_7d_only": "fires",
    "history_only": "hist",
    "location_only": "loc",
    "observation_only": "obs",
}


# --- 1. the backtest's field checks --------------------------------------------------------------------


def build(as_of: datetime, out: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """(field checks, pairs, timings): the backtest of the `risk` job, every pair kept."""
    season = job.last_season_fires(as_of)
    if season.reason:
        raise SystemExit(f"last season's fires unavailable: {season.reason} (set CACHE_DIR)")
    timings: dict[str, float] = {}
    with engine.connect() as conn, Session(bind=conn) as session:
        session.execute(text("SET work_mem = '8MB'"))  # the AWS server's setting
        t = time.perf_counter()
        burn_cols = job._columns(session, "burn_cluster")
        timing_cols = {"harvested_after", "harvested_before"} <= burn_cols
        windows = session.execute(text("""
            SELECT mgrs_tile, scope, burn_after, burn_before, count(*) AS n
            FROM burn_cluster WHERE ndvi_pre < :ndvi
            GROUP BY 1, 2, 3, 4 HAVING count(*) >= :min ORDER BY burn_before, mgrs_tile
        """), {"ndvi": model.HARVESTED_NDVI_MAX, "min": job.BACKTEST_MIN_EACH}).all()
        job._load_pairs(session, windows, "scope", timing_cols)
        counts = {int(p): (int(b), int(u)) for p, b, u in session.execute(text(
            "SELECT pair, sum(label), count(*) - sum(label) FROM bt GROUP BY pair")).all()}
        keep = [i for i in range(len(windows)) if min(counts.get(i, (0, 0))) >= job.BACKTEST_MIN_EACH]
        session.execute(text("DELETE FROM bt WHERE NOT (pair = ANY(:keep))"), {"keep": keep})
        timings["load_pairs_s"] = time.perf_counter() - t
        # timing enters a backtest score only where `days` (time since harvest) is known
        timings["checks_with_harvest_date"] = session.execute(text(
            "SELECT count(*) FILTER (WHERE days IS NOT NULL) FROM bt")).scalar_one()
        t = time.perf_counter()
        job._fill_backtest_factors(session, job.load_history(session, season))  # as the job does
        timings["factors_s"] = time.perf_counter() - t
        for stmt in model.score_sql("bt", "pair"):
            session.execute(text(stmt))
        t = time.perf_counter()
        per_pair = job._pair_metrics(session, True, timing_cols)
        timings["pair_metrics_s"] = time.perf_counter() - t
        t = time.perf_counter()
        rows = job._pooled_rows(session, None)
        timings["pooled_rows_s"] = time.perf_counter() - t
        session.rollback()
    pairs = pd.DataFrame([{"pair": i, "tile": w[0], "scope": w[1], "t0": w[2], "t1": w[3],
                           "burnt": counts[i][0], "unburnt": counts[i][1], "auc": per_pair[i]["auc"],
                           **{f"auc_{k}": v for k, v in per_pair[i]["baselines_auc"].items()}}
                          for i, w in enumerate(windows) if i in per_pair])
    rows.to_parquet(out / "field_checks.parquet")
    pairs.to_parquet(out / "pairs.parquet")
    meta = {k: round(v, 1) for k, v in timings.items()}
    (out / "build_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return rows, pairs, meta


# --- per-pair block matrices, summed over any set of pairs -----------------------------------------


class Mats:
    """Each pair's block concordance (C per score) and comparison counts (N), kept per pair on the
    blocks the pair touches, so the pooled numbers of any set of pairs are sums."""

    def __init__(self, rows: pd.DataFrame, scores: dict[str, str], block_km: float):
        self.block, self.k = model.block_ids(rows["x"].to_numpy(), rows["y"].to_numpy(), block_km)
        self.scores = scores
        self.per_pair: dict[int, tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]] = {}
        pair = rows["pair"].to_numpy()
        label = rows["label"].to_numpy()
        values = {name: rows[col].to_numpy(float) for name, col in scores.items()}
        order = np.argsort(pair, kind="stable")
        for idx in np.split(order, np.flatnonzero(np.diff(pair[order])) + 1):
            used, local = np.unique(self.block[idx], return_inverse=True)
            cs = {}
            n = None
            for name, v in values.items():
                cs[name], n = model.concordance_by_block(pair[idx], label[idx], local, v[idx], len(used))
            self.per_pair[int(pair[idx[0]])] = (used, cs, n)

    def total(self, pairs: list[int]) -> tuple[dict[str, np.ndarray], np.ndarray]:
        cs = {name: np.zeros((self.k, self.k)) for name in self.scores}
        n = np.zeros((self.k, self.k))
        for p in pairs:
            used, pc, pn = self.per_pair[p]
            ix = np.ix_(used, used)
            n[ix] += pn
            for name, c in cs.items():
                c[ix] += pc[name]
        return cs, n


def pooled(mats: Mats, pairs: list[int], against: str | None = "score", reps: int = model.POOLED_REPS,
           ) -> dict[str, Any]:
    cs, n = mats.total(pairs)
    points, draws = model.block_draws(cs, n, reps)
    out: dict[str, Any] = {"blocks_with_fields": int(((n.sum(axis=0) + n.sum(axis=1)) > 0).sum())}
    for name in cs:
        if points[name] is None:
            out[name] = None
            continue
        lo, hi = np.nanpercentile(draws[name], [2.5, 97.5])
        row = {"auc": points[name], "ci95": [float(lo), float(hi)]}
        if against and name != against:
            d = draws[name] - draws[against]
            d_lo, d_hi = np.nanpercentile(d, [2.5, 97.5])
            row["minus_" + against] = {"diff": points[name] - points[against], "ci95": [float(d_lo), float(d_hi)]}
        out[name] = row
    return out


# --- 2. the headline over the season, old and new ----------------------------------------------------


def stability(pairs: pd.DataFrame, mats: Mats, production: list[dict[str, Any]]) -> dict[str, Any]:
    days = pd.date_range(pairs["t1"].min().normalize() + timedelta(days=1), pairs["t1"].max().normalize()
                         + timedelta(days=1), freq="D", tz=UTC)
    series = []
    for d in days:
        known = pairs[pairs["t1"] < d]
        if known.empty:
            continue
        n = known["burnt"] + known["unburnt"]
        latest = known.assign(n=n).sort_values(["t1", "n"]).iloc[-1]  # the job's max(t1, n)
        newest = known["t1"].max()
        window = known[known["t1"] > newest - timedelta(days=model_days())]
        b, u = int(window["burnt"].sum()), int(window["unburnt"].sum())
        row: dict[str, Any] = {"day": str(d.date() - timedelta(days=1)), "old_auc": float(latest["auc"]),
                               "old_tile": latest["tile"], "old_burnt": int(latest["burnt"]),
                               "old_unburnt": int(latest["unburnt"]), "new_pairs": len(window),
                               "new_tiles": int(window["tile"].nunique()), "new_burnt": b, "new_unburnt": u}
        if b >= job.RECENT_MIN_EACH and u >= job.RECENT_MIN_EACH:
            p = pooled(mats, [int(x) for x in window["pair"]], against=None, reps=1000)["score"]
            row.update(new_auc=p["auc"], new_ci95=p["ci95"])
        series.append(row)
    df = pd.DataFrame(series)

    def spread(col: str, last: int | None = None) -> dict[str, Any]:
        x = df[col].dropna()
        if last:
            x = x.iloc[-last:]
        return {"days": len(x), "sd": round(float(x.std(ddof=1)), 4), "min": round(float(x.min()), 3),
                "max": round(float(x.max()), 3), "mean_abs_daily_change": round(float(x.diff().abs().mean()), 4)}

    widths = df["new_ci95"].dropna().map(lambda c: c[1] - c[0]) if "new_ci95" in df else pd.Series(dtype=float)
    return {
        "series": series,
        "old_latest_pair": {"season": spread("old_auc"), "last_14_days": spread("old_auc", 14)},
        "new_pooled_7d": {"season": spread("new_auc"), "last_14_days": spread("new_auc", 14),
                          "days_not_shown": int(df["new_auc"].isna().sum()) if "new_auc" in df else len(df),
                          "median_ci95_width": round(float(widths.median()), 4) if len(widths) else None},
        "production_served_headlines": {
            "runs": production, **served_spread(production, "latest_pair"),
            "recent_pooled": served_spread(production, "recent"),
            "note": ("the headline each production run of the risk job reported (job_run.result), as served: "
                     "sd/min/max over the runs that served the latest pair (before 2026-11-23), recent_pooled "
                     "over the runs that serve the pooled week"),
        },
    }


def model_days() -> int:
    return job.RECENT_DAYS


# --- 3. which order to serve ---------------------------------------------------------------------------


def iso_week(t: pd.Timestamp) -> str:
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


def which_order(pairs: pd.DataFrame, mats: Mats, checks_with_harvest_date: int | None) -> dict[str, Any]:
    pairs = pairs.assign(week=[iso_week(t) for t in pairs["t1"]])
    weeks = sorted(pairs["week"].unique())
    held = weeks[1:]  # forward-chaining: the first week only ever trains (nothing is fitted here)
    per_week = {}
    for w in weeks:
        ids = [int(x) for x in pairs.loc[pairs["week"] == w, "pair"]]
        sub = pairs[pairs["week"] == w]
        res = pooled(mats, ids, reps=1000)
        per_week[w] = {"pairs": len(ids), "burnt": int(sub["burnt"].sum()), "unburnt": int(sub["unburnt"].sum()),
                       **{k: _rnd(v) for k, v in res.items()}}
    ids = [int(x) for x in pairs.loc[pairs["week"].isin(held), "pair"]]
    pooled_held = pooled(mats, ids)
    s = pooled_held["score"]["auc"]
    ha = pooled_held["burned_ha_7d_only"]
    rule_ha = {"simple_auc": ha["auc"], "composite_auc": s, "diff": ha["minus_score"]["diff"],
               "diff_ci95": ha["minus_score"]["ci95"],
               "switch": bool(ha["auc"] >= s and ha["minus_score"]["ci95"][0] > -0.01)}
    # sensitivity: only pairs after 13 Nov, which the critique's look at the 13 Nov report (where
    # "scar hectares alone" first matched the composite) could not have seen
    late_ids = [int(x) for x in pairs.loc[pairs["t1"] > pd.Timestamp("2026-11-14", tz=UTC), "pair"]]
    late = pooled(mats, late_ids) if late_ids else None
    weeks_ha_ahead = sum(1 for w in held if per_week[w]["burned_ha_7d_only"] and per_week[w]["score"]
                         and per_week[w]["burned_ha_7d_only"]["auc"] >= per_week[w]["score"]["auc"])
    # The composite without timing: a backtest row's score uses timing only where its `days` (time
    # since harvest) is known, and _load_pairs leaves it NULL on every row unless burn_cluster records
    # harvest dates. With no row dated, the backtest's composite IS the composite without timing on every
    # field check, and the served composite (with timing) has no backtest AUC of its own.
    same = checks_with_harvest_date == 0 if checks_with_harvest_date is not None else None
    return {
        "weeks": weeks, "held_out_weeks": held, "per_week": per_week,
        "pooled_held_out": {k: _rnd(v) for k, v in pooled_held.items()},
        "held_out_weeks_where_hectares_ge_composite": f"{weeks_ha_ahead} of {len(held)}",
        "after_13_nov": None if late is None else {"pairs": len(late_ids), **{k: _rnd(v) for k, v in late.items()}},
        "rule": "switch to the simpler order only if its pooled held-out AUC >= the composite's and the "
                "paired block-bootstrap 95% CI of (simple - composite) has a lower bound > -0.01",
        "hectares_only_vs_composite": _rnd(rule_ha),
        "composite_without_timing_vs_composite": {
            "checks_with_harvest_date": checks_with_harvest_date,
            "identical_on_every_field_check": same,
            "why": "the backtest cannot know when a burnt field was harvested (burn_cluster stores no harvest "
                   "dates), so its composite has no timing factor: the served composite's timing is untested "
                   "and the rule has no held-out AUC of the served composite to compare against",
        },
    }


def _rnd(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, list):
        return [_rnd(x) for x in v]
    if isinstance(v, dict):
        return {k: _rnd(x) for k, x in v.items()}
    return v


def production_headlines() -> list[dict[str, Any]]:
    """The headline each production risk run stored in job_run.result.backtest. Runs before
    2026-11-23 stored the latest pair (top-level auc: kind "latest_pair"); later runs store the pooled
    week under recent (kind "recent"; burnt/unburnt are field checks)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT started_at, result -> 'backtest' FROM job_run
            WHERE name = 'risk' AND ok
              AND (result -> 'backtest' ? 'auc' OR result -> 'backtest' -> 'recent' ? 'auc')
            ORDER BY started_at
        """)).all()
    out = []
    for started, b in rows:
        if isinstance(b.get("recent"), dict):
            r = b["recent"]
            out.append({"run": started.isoformat(), "kind": "recent", "auc": r.get("auc"), "pairs": r.get("pairs"),
                        "tiles": r.get("tiles"), "burnt": r.get("burnt"), "unburnt": r.get("unburnt")})
        else:
            out.append({"run": started.isoformat(), "kind": "latest_pair", "tile": b.get("tile"), "auc": b.get("auc"),
                        "burnt": b.get("burnt"), "unburnt": b.get("unburnt")})
    return out


def served_spread(production: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """SD, min and max of the AUCs that production runs of one kind served."""
    aucs = [r["auc"] for r in production if r.get("kind") == kind and r.get("auc") is not None]
    return {"n_runs": len(aucs), "sd": round(float(np.std(aucs, ddof=1)), 4) if len(aucs) > 1 else None,
            "min": min(aucs) if aucs else None, "max": max(aucs) if aucs else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--as-of", default=None, help="the run time (default: the newest scar's pass)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--reuse", action="store_true", help="read field_checks.parquet from --out")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"database": engine.url.render_as_string(hide_password=True)}
    if args.reuse:
        rows = pd.read_parquet(out / "field_checks.parquet")
        pairs = pd.read_parquet(out / "pairs.parquet")
        meta = out / "build_meta.json"
        report["build_timings"] = json.loads(meta.read_text()) if meta.exists() else {}
    else:
        with engine.connect() as conn:
            newest = conn.execute(text("SELECT max(burn_before) FROM burn_cluster")).scalar_one()
        as_of = datetime.fromisoformat(args.as_of) if args.as_of else newest
        rows, pairs, report["build_timings"] = build(as_of, out)
    report["field_checks"] = len(rows)
    report["pairs"] = len(pairs)
    n = pairs["burnt"] + pairs["unburnt"]
    w = pairs["burnt"] * pairs["unburnt"]
    report["season_estimators"] = {  # the site's season figure (n-weighted) against the pooled one
        "n_weighted_mean_auc": round(float((pairs["auc"] * n).sum() / n.sum()), 4),
        "burnt_x_unburnt_weighted_auc": round(float((pairs["auc"] * w).sum() / w.sum()), 4),
        "per_pair_auc_range": [float(pairs["auc"].min()), float(pairs["auc"].max())],
        "pairs_below_0.5": int((pairs["auc"] < 0.5).sum()),
    }
    for km in (model.BLOCK_KM, 50.0):
        t = time.perf_counter()
        mats = Mats(rows, SCORES, km)
        key = f"blocks_{km:g}km"
        report[key] = {"blocks": mats.k, "matrices_s": round(time.perf_counter() - t, 1)}
        newest = pairs["t1"].max()
        recent = [int(x) for x in pairs.loc[pairs["t1"] > newest - timedelta(days=job.RECENT_DAYS), "pair"]]
        report[key]["recent_7d"] = _rnd(pooled(mats, recent))
        report[key]["season"] = _rnd(pooled(mats, [int(x) for x in pairs["pair"]]))
        report[key]["which_order"] = which_order(pairs, mats,
                                                 report["build_timings"].get("checks_with_harvest_date"))
        if km == model.BLOCK_KM:
            report["stability"] = _rnd(stability(pairs, mats, production_headlines()))
    (out / "pooled_eval.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "stability"}, indent=1, default=str)[:20000])
    st = report["stability"]
    print(json.dumps({k: v for k, v in st.items() if k != "series"}, indent=1, default=str))


if __name__ == "__main__":
    main()
