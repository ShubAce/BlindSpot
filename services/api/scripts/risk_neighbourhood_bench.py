"""Time the risk job's neighbourhood and history features, and check them against the geography
reference they replaced (blindspot/jobs/risk.py fill_neighbourhood, fill_history).

Run it on a writable COPY of a production snapshot, never on the live database: it builds the job's
temporary tables, so it needs no write to any real table, but it keeps a CPU busy for minutes.

    cd services/api
    DATABASE_URL=postgresql+psycopg://blindspot:blindspot@localhost:5434/eval_risk \
    CACHE_DIR=<a folder holding risk/firms_VIIRS_SNPP_SP_2025-09-15_2025-11-30.csv> \
    uv run python scripts/risk_neighbourhood_bench.py --as-of 2026-11-22T17:47:34Z \
        --variants reference,job --out <folder>

Variants (each fills fires, unseen, ha and hist on the job's own field table `rk`):
  reference  the code as it was until 2026-11-23: geography (metres on the spheroid), every scar's
             polygon, a LATERAL ST_DWithin per field (kept here verbatim, so the check can be rerun)
  job        whatever blindspot.jobs.risk.fill_neighbourhood / fill_history do now (with --backtest:
             _fill_backtest_factors, once per field and first pass, then copied to every check)
  slab       --backtest only: fill_neighbourhood on every check (in slabs of first passes), no dedup
  planar     planar EPSG:32643 metres, scar polygons, the same LATERAL joins
  centroid   planar EPSG:32643 metres, scar centroids instead of polygons (an approximation)

For every variant: wall time of the neighbourhood and history statements, and against the
reference: the share of fields whose every count is equal, Spearman of each feature and of the
risk-v3 score (rain left out of both), and the overlap of the score's top 1%. With --backtest it
also fills the backtest table `bt` (every pass pair of the season, as the job does) per t0 and
compares the per-pair AUCs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics import risk as model
from blindspot.db import engine
from blindspot.jobs import risk as job

UTM = model.UTM_EPSG


# --- the reference: jobs/risk.py as of 1d192d0 (geography), verbatim ---------------------------------


def reference_fill_neighbourhood(session: Session, table: str, t_end: datetime, where: str = "TRUE",
                                 params: dict[str, Any] | None = None, days: int = model.NEIGHBOUR_DAYS,
                                 radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    window = {"t0": t_end - timedelta(days=days), "t1": t_end}
    session.execute(text("DROP TABLE IF EXISTS nb_fire, nb_burn"))
    session.execute(text("""
        CREATE TEMP TABLE nb_fire ON COMMIT DROP AS
        SELECT geom::geography AS g FROM fire_detection
        WHERE acq_utc > :t0 AND acq_utc <= :t1 AND confidence <> 'low' AND NOT static_source
    """ if _has_static(session) else """
        CREATE TEMP TABLE nb_fire ON COMMIT DROP AS
        SELECT geom::geography AS g FROM fire_detection
        WHERE acq_utc > :t0 AND acq_utc <= :t1 AND confidence <> 'low'
    """), window)
    session.execute(text("""
        CREATE TEMP TABLE nb_burn ON COMMIT DROP AS
        SELECT geom::geography AS g, area_ha, unseen FROM burn_cluster
        WHERE burn_before > :t0 AND burn_before <= :t1
    """), window)
    for name in ("nb_fire", "nb_burn"):
        session.execute(text(f"CREATE INDEX ON {name} USING gist (g)"))
        session.execute(text(f"ANALYZE {name}"))
    session.execute(text(f"""
        UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
        FROM (
            SELECT p.k, f.n, b.unseen, b.ha
            FROM {table} p
            CROSS JOIN LATERAL (SELECT count(*) AS n FROM nb_fire f WHERE ST_DWithin(f.g, p.g, :m)) f
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                FROM nb_burn b WHERE ST_DWithin(b.g, p.g, :m)
            ) b
            WHERE {where}
        ) AS x
        WHERE t.k = x.k
    """), {"m": radius_km * 1000, **(params or {})})
    session.execute(text("DROP TABLE nb_fire, nb_burn"))


def reference_fill_history(session: Session, table: str, radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    session.execute(text(f"""
        UPDATE {table} AS t SET hist = (SELECT count(*) FROM rk_hist h WHERE ST_DWithin(h.g, t.g, :m))
    """), {"m": radius_km * 1000})


def _has_static(session: Session) -> bool:
    return "static_source" in job._columns(session, "fire_detection")


# --- planar variants (for the record: what was measured before choosing) -----------------------------


def planar_fill_neighbourhood(session: Session, table: str, t_end: datetime, where: str = "TRUE",
                              params: dict[str, Any] | None = None, centroids: bool = False,
                              days: int = model.NEIGHBOUR_DAYS,
                              radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    window = {"t0": t_end - timedelta(days=days), "t1": t_end}
    static = "AND NOT static_source" if _has_static(session) else ""
    session.execute(text("DROP TABLE IF EXISTS nb_fire, nb_burn"))
    session.execute(text(f"""
        CREATE TEMP TABLE nb_fire ON COMMIT DROP AS
        SELECT ST_Transform(geom, {UTM}) AS g FROM fire_detection
        WHERE acq_utc > :t0 AND acq_utc <= :t1 AND confidence <> 'low' {static}
    """), window)
    shape = "centroid" if centroids else "geom"
    session.execute(text(f"""
        CREATE TEMP TABLE nb_burn ON COMMIT DROP AS
        SELECT ST_Transform({shape}, {UTM}) AS g, area_ha, unseen FROM burn_cluster
        WHERE burn_before > :t0 AND burn_before <= :t1
    """), window)
    for name in ("nb_fire", "nb_burn"):
        session.execute(text(f"CREATE INDEX ON {name} USING gist (g)"))
        session.execute(text(f"ANALYZE {name}"))
    session.execute(text(f"""
        UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
        FROM (
            SELECT p.k, f.n, b.unseen, b.ha
            FROM {table} p
            CROSS JOIN LATERAL (SELECT ST_Transform(p.g::geometry, {UTM}) AS q) q
            CROSS JOIN LATERAL (SELECT count(*) AS n FROM nb_fire f WHERE ST_DWithin(f.g, q.q, :m)) f
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                FROM nb_burn b WHERE ST_DWithin(b.g, q.q, :m)
            ) b
            WHERE {where}
        ) AS x
        WHERE t.k = x.k
    """), {"m": radius_km * 1000, **(params or {})})
    session.execute(text("DROP TABLE nb_fire, nb_burn"))


def twostage_fill_neighbourhood(session: Session, table: str, t_end: datetime, where: str = "TRUE",
                                params: dict[str, Any] | None = None, days: int = model.NEIGHBOUR_DAYS,
                                radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    """Planar polygons, exactly, but a scar's polygon is only read when its centroid cannot decide:
    with c its anchor point, e = distance(c, polygon) and r = the farthest polygon point from c, a
    scar is within m of q when d(q, c) + e <= m, and beyond it when d(q, c) - r > m."""
    window = {"t0": t_end - timedelta(days=days), "t1": t_end}
    static = "AND NOT static_source" if _has_static(session) else ""
    session.execute(text("DROP TABLE IF EXISTS nb_fire, nb_burn"))
    session.execute(text(f"""
        CREATE TEMP TABLE nb_fire ON COMMIT DROP AS
        SELECT ST_Transform(geom, {UTM}) AS g FROM fire_detection
        WHERE acq_utc > :t0 AND acq_utc <= :t1 AND confidence <> 'low' {static}
    """), window)
    session.execute(text(f"""
        CREATE TEMP TABLE nb_burn ON COMMIT DROP AS
        SELECT g, c, ST_Distance(c, g) AS e, ST_MaxDistance(c, g) AS r, area_ha, unseen
        FROM (SELECT ST_Transform(geom, {UTM}) AS g, ST_Transform(centroid, {UTM}) AS c, area_ha, unseen
              FROM burn_cluster WHERE burn_before > :t0 AND burn_before <= :t1) s
    """), window)
    for name in ("nb_fire", "nb_burn"):
        session.execute(text(f"CREATE INDEX ON {name} USING gist (g)"))
        session.execute(text(f"ANALYZE {name}"))
    session.execute(text(f"""
        UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
        FROM (
            SELECT p.k, f.n, b.unseen, b.ha
            FROM {table} p
            CROSS JOIN LATERAL (SELECT ST_Transform(p.g::geometry, {UTM}) AS q) q
            CROSS JOIN LATERAL (SELECT count(*) AS n FROM nb_fire f WHERE ST_DWithin(f.g, q.q, :m)) f
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                FROM nb_burn b
                WHERE b.g && ST_Expand(q.q, :m)
                  AND (ST_Distance(b.c, q.q) + b.e <= :m
                       OR (ST_Distance(b.c, q.q) - b.r <= :m AND ST_DWithin(b.g, q.q, :m)))
            ) b
            WHERE {where}
        ) AS x
        WHERE t.k = x.k
    """), {"m": radius_km * 1000, **(params or {})})
    session.execute(text("DROP TABLE nb_fire, nb_burn"))


def _season_tables(session: Session) -> None:
    """The season's fires and scars once, projected to UTM 43N, each with its time (t) and a 3-D box
    (bx: a line from (xmin, ymin, day) to (xmax, ymax, day)) for an n-D index over space and time."""
    static = "AND NOT static_source" if _has_static(session) else ""
    session.execute(text("DROP TABLE IF EXISTS sea_fire, sea_burn"))
    session.execute(text(f"""
        CREATE TEMP TABLE sea_fire ON COMMIT DROP AS
        SELECT g, t, ST_MakeLine(ST_MakePoint(ST_X(g), ST_Y(g), d), ST_MakePoint(ST_X(g), ST_Y(g), d)) AS bx
        FROM (SELECT ST_Transform(geom, {UTM}) AS g, acq_utc AS t, extract(epoch FROM acq_utc) / 86400.0 AS d
              FROM fire_detection WHERE confidence <> 'low' {static}) s
    """))
    session.execute(text(f"""
        CREATE TEMP TABLE sea_burn ON COMMIT DROP AS
        SELECT g, t, area_ha, unseen,
               ST_MakeLine(ST_MakePoint(ST_XMin(g), ST_YMin(g), d), ST_MakePoint(ST_XMax(g), ST_YMax(g), d)) AS bx
        FROM (SELECT ST_Transform(geom, {UTM}) AS g, burn_before AS t, area_ha, unseen,
                     extract(epoch FROM burn_before) / 86400.0 AS d FROM burn_cluster) s
    """))


def loop_fill_neighbourhood(session: Session, table: str, days: int = model.NEIGHBOUR_DAYS,
                            radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    """(C) one statement per first pass, its window's rows copied from the season, projected once."""
    _season_tables(session)
    for (t0,) in session.execute(text(f"SELECT DISTINCT t0 FROM {table} ORDER BY 1")).all():
        w = {"a": t0 - timedelta(days=days), "b": t0}
        session.execute(text("DROP TABLE IF EXISTS nb_fire, nb_burn"))
        session.execute(text("CREATE TEMP TABLE nb_fire ON COMMIT DROP AS SELECT g FROM sea_fire WHERE t > :a AND t <= :b"), w)
        session.execute(text("CREATE TEMP TABLE nb_burn ON COMMIT DROP AS SELECT g, area_ha, unseen FROM sea_burn "
                             "WHERE t > :a AND t <= :b"), w)
        for name in ("nb_fire", "nb_burn"):
            session.execute(text(f"CREATE INDEX ON {name} USING gist (g)"))
            session.execute(text(f"ANALYZE {name}"))
        session.execute(text(f"""
            UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
            FROM (
                SELECT p.k, f.n, b.unseen, b.ha
                FROM {table} p
                CROSS JOIN LATERAL (SELECT ST_Transform(p.g::geometry, {UTM}) AS q) q
                CROSS JOIN LATERAL (SELECT count(*) AS n FROM nb_fire f WHERE ST_DWithin(f.g, q.q, :m)) f
                CROSS JOIN LATERAL (
                    SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                    FROM nb_burn b WHERE ST_DWithin(b.g, q.q, :m)) b
                WHERE p.t0 = :t0
            ) AS x WHERE t.k = x.k
        """), {"m": radius_km * 1000, "t0": t0})


def nd_fill_neighbourhood(session: Session, table: str, days: int = model.NEIGHBOUR_DAYS,
                          radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    """(D) one statement; an n-D GiST index over (x, y, day) finds each row's nearby rows of its own
    window at once."""
    _season_tables(session)
    for name in ("sea_fire", "sea_burn"):
        session.execute(text(f"CREATE INDEX ON {name} USING gist (bx gist_geometry_ops_nd)"))
        session.execute(text(f"ANALYZE {name}"))
    session.execute(text(f"""
        UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
        FROM (
            SELECT p.k, f.n, b.unseen, b.ha
            FROM {table} p
            CROSS JOIN LATERAL (
                SELECT q, ST_MakeLine(ST_MakePoint(ST_X(q) - :m, ST_Y(q) - :m, d - :days),
                                      ST_MakePoint(ST_X(q) + :m, ST_Y(q) + :m, d)) AS box
                FROM (SELECT ST_Transform(p.g::geometry, {UTM}) AS q, extract(epoch FROM p.t0) / 86400.0 AS d) s
            ) q
            CROSS JOIN LATERAL (SELECT count(*) AS n FROM sea_fire f
                                WHERE f.bx &&& q.box AND f.t > p.t0 - :span AND f.t <= p.t0
                                  AND ST_DWithin(f.g, q.q, :m)) f
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                FROM sea_burn b WHERE b.bx &&& q.box AND b.t > p.t0 - :span AND b.t <= p.t0
                  AND ST_DWithin(b.g, q.q, :m)) b
        ) AS x WHERE t.k = x.k
    """), {"m": radius_km * 1000, "days": float(days), "span": timedelta(days=days)})


def season_fill_neighbourhood(session: Session, table: str, days: int = model.NEIGHBOUR_DAYS,
                              radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    """(B) one statement over the season's fires and scars, each candidate's time tested against the
    row's own t0 (the job's first version of 2026-11-23)."""
    _season_tables(session)
    for name in ("sea_fire", "sea_burn"):
        session.execute(text(f"CREATE INDEX ON {name} USING gist (g)"))
        session.execute(text(f"ANALYZE {name}"))
    session.execute(text(f"""
        UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
        FROM (
            SELECT p.k, f.n, b.unseen, b.ha
            FROM {table} p
            CROSS JOIN LATERAL (SELECT ST_Transform(p.g::geometry, {UTM}) AS q) q
            CROSS JOIN LATERAL (SELECT count(*) AS n FROM sea_fire f WHERE ST_DWithin(f.g, q.q, :m)
                                AND f.t > p.t0 - :span AND f.t <= p.t0) f
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                FROM sea_burn b WHERE ST_DWithin(b.g, q.q, :m) AND b.t > p.t0 - :span AND b.t <= p.t0) b
        ) AS x WHERE t.k = x.k
    """), {"m": radius_km * 1000, "span": timedelta(days=days)})


VARIANTS = {
    "reference": (reference_fill_neighbourhood, reference_fill_history),
    "job": (job.fill_neighbourhood, None),  # history: job.fill_history (needs the History)
    "planar": (planar_fill_neighbourhood, None),
    "centroid": (lambda s, t, e, *a, **k: planar_fill_neighbourhood(s, t, e, *a, centroids=True, **k), None),
    "twostage": (twostage_fill_neighbourhood, None),
    "slab": (None, None),  # backtest only: fill_neighbourhood on every check, no dedup
    "season_loop": (None, None),  # backtest only
    "nd": (None, None),  # backtest only
    "season_single": (None, None),  # backtest only
}


def _features(session: Session, table: str, key: str = "k") -> pd.DataFrame:
    rows = session.execute(text(f"SELECT {key}, fires, unseen, ha, hist FROM {table}")).all()
    return pd.DataFrame(rows, columns=["k", "fires", "unseen", "ha", "hist"]).set_index("k")


def _score(session: Session, feats: pd.DataFrame, days: pd.Series) -> pd.Series:
    """risk-v3 scores of these features with model.score_sql (rain left out), one run's typicals."""
    session.execute(text("DROP TABLE IF EXISTS bench_s"))
    session.execute(text(f"CREATE TEMP TABLE bench_s ({job.FIELD_COLUMNS}{model.SCORE_COLUMNS}) ON COMMIT DROP"))
    driver = session.connection().connection.driver_connection
    with driver.cursor() as cur, cur.copy("COPY bench_s (k, days, fires, unseen, ha, hist) FROM STDIN") as copy:
        for k, fires, unseen, ha, hist in feats[["fires", "unseen", "ha", "hist"]].itertuples(name=None):
            d = days.get(k)
            copy.write_row((int(k), None if d is None or pd.isna(d) else float(d), int(fires), int(unseen),
                            float(ha), None if pd.isna(hist) else int(hist)))
    for stmt in model.score_sql("bench_s"):
        session.execute(text(stmt))
    rows = session.execute(text("SELECT k, s FROM bench_s")).all()
    session.execute(text("DROP TABLE bench_s"))
    return pd.Series({int(k): float(s) for k, s in rows}).sort_index()


def _top(s: pd.Series, share: float = 0.01) -> set[int]:
    """Fields whose score is matched or beaten by at most `share` of the run (cume_dist, as the job)."""
    ranks = s.rank(ascending=False, method="max")
    return set(s.index[ranks <= share * len(s)])


def compare(ref: pd.DataFrame, new: pd.DataFrame, s_ref: pd.Series | None = None,
            s_new: pd.Series | None = None) -> dict[str, Any]:
    new = new.reindex(ref.index)
    out: dict[str, Any] = {"fields": len(ref)}
    for col in ("fires", "unseen", "ha", "hist"):
        a, b = ref[col].astype(float), new[col].astype(float)
        both = a.notna() & b.notna()
        out[col] = {
            "equal_share": round(float((np.isclose(a[both], b[both], rtol=0, atol=1e-9)).mean()), 6),
            "spearman": None if a[both].nunique() < 2 else round(float(spearmanr(a[both], b[both]).statistic), 6),
            "mean_ref": round(float(a.mean()), 4), "mean_new": round(float(b.mean()), 4),
            "median_abs_rel_err": round(float((np.abs(b[both] - a[both]) / np.maximum(a[both], 1e-9))[a[both] > 0]
                                              .median()), 6) if (a[both] > 0).any() else None,
            "max_abs_err": round(float(np.abs(b[both] - a[both]).max()), 4),
        }
    out["all_counts_equal_share"] = round(float(
        (ref[["fires", "unseen", "hist"]].fillna(-1).values == new[["fires", "unseen", "hist"]].fillna(-1).values)
        .all(axis=1).mean()), 6)
    if s_ref is not None and s_new is not None:
        s_new = s_new.reindex(s_ref.index)
        top_ref, top_new = _top(s_ref), _top(s_new)
        out["score"] = {"spearman": round(float(spearmanr(s_ref, s_new).statistic), 6),
                        "top_1pct_overlap": round(len(top_ref & top_new) / max(1, len(top_ref)), 4),
                        "top_1pct_fields": len(top_ref),
                        "max_abs_diff": round(float(np.abs(s_new - s_ref).max()), 3)}
    return out


def run_scoring(as_of: datetime, variants: list[str], out: Path, reuse: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"as_of": as_of.isoformat(), "variants": {}}
    frames: dict[str, pd.DataFrame] = {}
    scores: dict[str, pd.Series] = {}
    season = job.last_season_fires(as_of)
    if season.reason:
        raise SystemExit(f"last season's fires unavailable: {season.reason} (set CACHE_DIR)")
    for name in variants:
        cached = out / f"rk_{name}.parquet"
        if name == "reference" and reuse and cached.exists():  # the slow one: measured once, kept
            frames[name] = pd.read_parquet(cached)
            report["variants"][name] = json.loads((out / "rk_reference.json").read_text())
            with engine.connect() as conn, Session(bind=conn) as session:
                job._load_field_table(session, as_of)
                days = pd.Series(dict(session.execute(text("SELECT k, days FROM rk")).all()))
                scores[name] = _score(session, frames[name], days)
                session.rollback()
            print(name, "(reused)", report["variants"][name], flush=True)
            continue
        with engine.connect() as conn, Session(bind=conn) as session:
            session.execute(text("SET work_mem = '8MB'"))  # the AWS server's setting (infra/aws/setup_server.sh)
            n = job._load_field_table(session, as_of)
            history = job.load_history(session, season)
            fill, fill_hist = VARIANTS[name]
            t = time.perf_counter()
            fill(session, "rk", as_of)
            t_nb = time.perf_counter() - t
            t = time.perf_counter()
            if fill_hist is None:
                job.fill_history(session, "rk", history)
            else:
                fill_hist(session, "rk")
            t_hist = time.perf_counter() - t
            report["variants"][name] = {"fields": n, "neighbourhood_s": round(t_nb, 2), "history_s": round(t_hist, 2)}
            print(name, report["variants"][name], flush=True)
            frames[name] = _features(session, "rk")
            frames[name].to_parquet(cached)
            if name == "reference":
                (out / "rk_reference.json").write_text(json.dumps(report["variants"][name]))
            days = pd.Series(dict(session.execute(text("SELECT k, days FROM rk")).all()))
            scores[name] = _score(session, frames[name], days)
            session.rollback()
    if "reference" in frames:
        for name in variants:
            if name != "reference":
                report["variants"][name]["vs_reference"] = compare(frames["reference"], frames[name],
                                                                   scores["reference"], scores[name])
    if "planar" in frames and "centroid" in frames:
        report["centroid_vs_planar_polygons"] = compare(frames["planar"], frames["centroid"],
                                                        scores["planar"], scores["centroid"])
    return report


def run_backtest(as_of: datetime, variants: list[str], out: Path) -> dict[str, Any]:
    """Every pass pair's fields into `bt` (as the job's backtest does), then each variant's
    neighbourhood per t0 and history; per-pair AUCs of the score against the reference's."""
    report: dict[str, Any] = {"variants": {}}
    season = job.last_season_fires(as_of)
    aucs: dict[str, dict[int, Any]] = {}
    frames: dict[str, pd.DataFrame] = {}
    for name in variants:
        with engine.connect() as conn, Session(bind=conn) as session:
            session.execute(text("SET work_mem = '8MB'"))
            burn_cols = job._columns(session, "burn_cluster")
            windows = session.execute(text("""
                SELECT mgrs_tile, scope, burn_after, burn_before, count(*) AS n
                FROM burn_cluster WHERE ndvi_pre < :ndvi
                GROUP BY 1, 2, 3, 4 HAVING count(*) >= :min ORDER BY burn_before, mgrs_tile
            """), {"ndvi": model.HARVESTED_NDVI_MAX, "min": job.BACKTEST_MIN_EACH}).all()
            job._load_pairs(session, windows, "scope", {"harvested_after", "harvested_before"} <= burn_cols)
            counts = {int(p): (int(b), int(u)) for p, b, u in session.execute(text(
                "SELECT pair, sum(label), count(*) - sum(label) FROM bt GROUP BY pair")).all()}
            keep = [i for i in range(len(windows)) if min(counts.get(i, (0, 0))) >= job.BACKTEST_MIN_EACH]
            session.execute(text("DELETE FROM bt WHERE NOT (pair = ANY(:keep))"), {"keep": keep})
            if "t0" not in {r[0] for r in session.execute(text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                    "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'bt'::regclass")).all()}:
                session.execute(text("CREATE INDEX ON bt (t0)"))  # the per-t0 loops filter on it
            session.execute(text("ANALYZE bt"))
            rows = session.execute(text("SELECT count(*), count(DISTINCT t0) FROM bt")).one()
            fill, fill_hist = VARIANTS["job" if name in ("job_loop", "slab") else name]
            t = time.perf_counter()
            if name == "job":  # the job's own way: once per (field, t0) and per field, then copied
                job._fill_backtest_factors(session, job.load_history(session, season))
            elif name == "slab":  # every check, each as of its own t0, in slabs of first passes
                job.fill_neighbourhood(session, "bt", None)
            elif name == "season_loop":
                loop_fill_neighbourhood(session, "bt")
            elif name == "nd":
                nd_fill_neighbourhood(session, "bt")
            elif name == "season_single":
                season_fill_neighbourhood(session, "bt")
            else:  # one statement per first pass (as the job did until 2026-11-23)
                for (t0,) in session.execute(text("SELECT DISTINCT t0 FROM bt ORDER BY 1")).all():
                    fill(session, "bt", t0, "p.t0 = :t0", {"t0": t0})
            t_nb = time.perf_counter() - t
            loaded = job.load_history(session, season)
            t = time.perf_counter()
            if name == "job":
                pass  # last season's fires are in _fill_backtest_factors, timed above
            elif fill_hist is None:
                job.fill_history(session, "bt", loaded)
            else:
                fill_hist(session, "bt")
            t_hist = time.perf_counter() - t
            for stmt in model.score_sql("bt", "pair"):
                session.execute(text(stmt))
            aucs[name] = {p: m["auc"] for p, m in job._pair_metrics(session, True).items()}
            # rows keyed by (pair, field, label): bt's serial k depends on insertion order
            frames[name] = _features(session, "bt", key="CAST(pair AS bigint) * 10000000000 + fid * 2 + label")
            session.rollback()
        report["variants"][name] = {"field_pairs": int(rows[0]), "t0s": int(rows[1]),
                                    "neighbourhood_s": round(t_nb, 2), "history_s": round(t_hist, 2)}
        print(name, report["variants"][name], flush=True)
        # kept, so variants run in separate processes (at the same time) can be compared afterwards
        frames[name].to_parquet(out / f"bt_features_{name}.parquet")
        (out / f"bt_run_{name}.json").write_text(json.dumps(
            {"run": report["variants"][name], "pairs": {str(p): a for p, a in aucs[name].items()}}))
    return report | compare_backtests(variants, aucs, frames)


def compare_backtests(variants: list[str], aucs: dict[str, dict[int, Any]],
                      frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Per-pair AUC differences and feature agreement of each variant against the reference."""
    report: dict[str, Any] = {"vs_reference": {}}
    if "reference" in aucs:
        for name in variants:
            if name == "reference":
                continue
            diffs = [abs((aucs[name][p] or 0.5) - (aucs["reference"][p] or 0.5))
                     for p in aucs["reference"] if p in aucs[name]]
            report["vs_reference"][name] = {
                "pairs": len(diffs), "max_abs_auc_diff": round(max(diffs), 4) if diffs else None,
                "pairs_within_0.002": sum(d <= 0.002 + 1e-9 for d in diffs),
                "features": compare(frames["reference"], frames[name]),
            }
    return report


def compare_saved(variants: list[str], out: Path) -> dict[str, Any]:
    """compare_backtests() on what earlier --backtest runs saved in `out` (bt_run_*, bt_features_*)."""
    aucs, frames, runs = {}, {}, {}
    for name in variants:
        saved = json.loads((out / f"bt_run_{name}.json").read_text())
        runs[name] = saved["run"]
        aucs[name] = {int(p): a for p, a in saved["pairs"].items()}
        frames[name] = pd.read_parquet(out / f"bt_features_{name}.parquet")
    return {"variants": runs} | compare_backtests(variants, aucs, frames)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--as-of", required=True, help="the run time to score at, e.g. 2026-11-22T17:47:34Z")
    ap.add_argument("--variants", default="reference,job")
    ap.add_argument("--backtest", action="store_true", help="also fill the backtest's field-pairs")
    ap.add_argument("--skip-scoring", action="store_true")
    ap.add_argument("--reuse-reference", action="store_true",
                    help="read the reference features (and time) saved by an earlier run in --out")
    ap.add_argument("--out", default=".", help="folder for the report and the per-field features")
    ap.add_argument("--compare-saved", action="store_true",
                    help="no database work: compare the backtest variants saved in --out by earlier runs")
    args = ap.parse_args()
    as_of = datetime.fromisoformat(args.as_of)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    if args.compare_saved:
        report = compare_saved(variants, out)
        (out / f"bench_{'_'.join(variants)}_bt_saved.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(json.dumps(report, indent=1))
        return
    report: dict[str, Any] = {"database": engine.url.render_as_string(hide_password=True),
                              "server": None, "argv": sys.argv[1:]}
    with engine.connect() as conn:
        report["server"] = conn.execute(text("SELECT version()")).scalar_one()
        report["postgis"] = conn.execute(text("SELECT postgis_lib_version()")).scalar_one()
    if not args.skip_scoring:
        report["scoring"] = run_scoring(as_of, variants, out, args.reuse_reference)
    if args.backtest:
        report["backtest"] = run_backtest(as_of, variants, out)
    name = f"bench_{'_'.join(variants)}{'_bt' if args.backtest else ''}.json"
    (out / name).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    os.environ.setdefault("ENABLE_SCHEDULER", "0")
    main()
