"""Job `prune` (daily): delete what nothing needs any more, so the hosted database (Neon free tier,
0.5 GB) stays small through the season.

What goes, and why it is safe:
- smoke_run: every run except the newest of each (kind, city). GET /api/smoke/*, /api/met/now,
  the headline and the agent read only the newest run of a kind and city; smoke_validate reads
  winds and air, not old smoke runs. (The smoke job itself keeps 14 days; this keeps one.)
- met_run, with its met_grid rows (ON DELETE CASCADE): every run except the newest of each model.
  Each run holds 7 days of past hours plus the forecast, so the newest one covers the 72 h
  back-trajectories, the validation hindcast and the weather routes, which all read only the
  newest run of each model (models/met.py latest_runs). A smoke run's met_run_ids are a record of
  what it used; nothing reads the grids back through them.
- risk_run: runs that no harvest_cluster row points at, except the newest and the newest with a
  backtest (what GET /api/risk serves).
- job_run older than 30 days.
- source_fetch older than 30 days that no row points at: no fetch_id column of any table and no
  `fetch_id` / `fetch_ids` key anywhere in any JSON column (citations in agent runs, bulletins,
  smoke and risk runs...). Provenance of every number still shown stays.

Season data is never touched: fire_detection, official fire events, burn_cluster,
harvest_cluster, air_obs, geostationary pixels, boundaries and places.

PRUNE_DRY_RUN=1 makes the job count what it would delete (rows and bytes) and delete nothing:
`PRUNE_DRY_RUN=1 uv run python -m blindspot.jobs run prune`.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from blindspot.db import engine, session_scope
from blindspot.jobs import job
from blindspot.provenance import utcnow

log = logging.getLogger(__name__)

KEEP_DAYS = 30  # job_run and unreferenced source_fetch rows older than this go

# The rows each rule keeps, as SELECTs of ids (infra/load_neon.py copies only these, too).
KEEP_SMOKE_RUNS = """
    SELECT DISTINCT ON (kind, lower(coalesce(city, ''))) id FROM smoke_run
    ORDER BY kind, lower(coalesce(city, '')), run_at DESC, id DESC
"""
KEEP_MET_RUNS = """
    SELECT DISTINCT ON (model) id FROM met_run ORDER BY model, run_at DESC, id DESC
"""
KEEP_RISK_RUNS = """
    (SELECT id FROM risk_run ORDER BY as_of DESC, id DESC LIMIT 1)
    UNION
    (SELECT id FROM risk_run WHERE backtest IS NOT NULL ORDER BY as_of DESC, id DESC LIMIT 1)
    UNION
    SELECT DISTINCT (risk_factors ->> 'run')::bigint FROM harvest_cluster WHERE risk_factors ? 'run'
"""

# Any `fetch_id` / `fetch_ids` / `wikidata_fetch_id`... key at any depth of a JSON value, and the
# numbers under it (a number, a list, or a dict of lists).
_JSON_KEYS = 'strict $.** ? (@.type() == "object").keyvalue() ? (@.key like_regex "fetch_ids?$").value'
_JSON_NUMBERS = 'lax $.** ? (@.type() == "number")'


@job("prune", cron="0 12 * * *")
def prune() -> dict[str, Any]:
    """Daily: delete old smoke runs, met grids, risk runs, job runs and unreferenced fetches."""
    return run_prune(dry_run=os.environ.get("PRUNE_DRY_RUN", "").strip().lower() in ("1", "true", "yes"))


def run_prune(dry_run: bool = False) -> dict[str, Any]:
    """Delete (or with dry_run, only count) what the rules above let go, in one transaction."""
    cutoff = utcnow() - timedelta(days=KEEP_DAYS)
    with session_scope() as session:
        doomed = {
            "smoke_run": _ids(session, "smoke_run", f"id NOT IN ({KEEP_SMOKE_RUNS})"),
            "met_run": _ids(session, "met_run", f"id NOT IN ({KEEP_MET_RUNS})"),
            "risk_run": _ids(session, "risk_run", f"id NOT IN ({KEEP_RISK_RUNS})"),
            "job_run": _ids(session, "job_run", "started_at < :cutoff", {"cutoff": cutoff}),
            "source_fetch": _unreferenced_fetches(session, cutoff),
        }
        summary: dict[str, Any] = {"dry_run": dry_run, "keep_days": KEEP_DAYS}
        for table, ids in doomed.items():
            summary[table] = {"rows": len(ids), "bytes": _row_bytes(session, table, ids)}
        met_grid = session.execute(text("SELECT count(*), coalesce(sum(pg_column_size(g.*)), 0) FROM met_grid g "
                                        "WHERE run_id = ANY(:ids)"), {"ids": doomed["met_run"]}).one()
        summary["met_grid"] = {"rows": met_grid[0], "bytes": int(met_grid[1]), "via": "met_run (cascade)"}
        if not dry_run:
            # job_run before source_fetch: the fetch rule above already ignored job_run rows it deletes.
            for table in ("smoke_run", "met_run", "risk_run", "job_run", "source_fetch"):
                if doomed[table]:
                    session.execute(text(f"DELETE FROM {table} WHERE id = ANY(:ids)"), {"ids": doomed[table]})
    if not dry_run:
        touched = [t for t in ("smoke_run", "met_grid", "met_run", "source_fetch") if summary[t]["rows"]]
        for table in touched:
            _vacuum(table)
        summary["vacuumed"] = touched
    summary["database_bytes"] = _database_bytes()
    return summary


def _exists(session: Session, table: str) -> bool:
    return session.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}).scalar_one()


def _ids(session: Session, table: str, where: str, params: dict[str, Any] | None = None) -> list[int]:
    if not _exists(session, table):
        return []
    return list(session.execute(text(f"SELECT id FROM {table} WHERE {where}"), params or {}).scalars())


def _row_bytes(session: Session, table: str, ids: list[int]) -> int:
    if not ids:
        return 0
    return int(session.execute(text(f"SELECT coalesce(sum(pg_column_size(t.*)), 0) FROM {table} t "
                                    "WHERE id = ANY(:ids)"), {"ids": ids}).scalar_one())


def _unreferenced_fetches(session: Session, cutoff: Any) -> list[int]:
    """source_fetch ids older than `cutoff` that no column and no JSON value of any table names.
    Rows of job_run older than the cutoff do not count: they are deleted in the same run."""
    candidates = set(session.execute(text("SELECT id FROM source_fetch WHERE started_at < :c"),
                                     {"c": cutoff}).scalars())
    if not candidates:
        return []
    columns = session.execute(text("""
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name <> 'source_fetch'
          AND ((column_name LIKE '%fetch_id' AND data_type IN ('bigint', 'integer')) OR data_type = 'jsonb')
    """)).all()
    for table, column, data_type in columns:
        if not candidates:
            break
        skip_old_jobs = "AND t.started_at >= :c" if table == "job_run" else ""
        if data_type == "jsonb":
            sql = f"""SELECT DISTINCT (n #>> '{{}}')::numeric::bigint FROM {table} t,
                          jsonb_path_query(t.{column}, :keys) v, jsonb_path_query(v, :nums) n
                      WHERE t.{column} IS NOT NULL {skip_old_jobs}"""
        else:
            sql = f"SELECT DISTINCT t.{column} FROM {table} t WHERE t.{column} = ANY(:ids) {skip_old_jobs}"
        used = session.execute(text(sql), {"keys": _JSON_KEYS, "nums": _JSON_NUMBERS, "ids": list(candidates),
                                           "c": cutoff}).scalars()
        candidates -= {int(i) for i in used if i is not None}
    return sorted(candidates)


def _vacuum(table: str) -> None:
    """VACUUM (not FULL): the freed space is reused at once and no lock blocks the readers."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"VACUUM (ANALYZE) {table}"))


def _database_bytes() -> int | None:
    try:
        with session_scope() as session:
            return int(session.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
    except SQLAlchemyError:  # only a figure for the summary
        return None
