"""Give Neon back the space that the daily rebuilds leave behind, without deleting any row.

The Sentinel-2 job rebuilds burn_cluster and harvest_cluster and the risk job rewrites every
harvested field, so each run leaves a dead copy of those tables inside their files. Postgres reuses
that room but never returns it, and on 2026-10-31 the database reached 949 MB of Neon's 1 GB free
storage with about 420 MB of live data. At the cap Neon refuses every write (jobs, stored answers).

For each table this drops its secondary indexes (that frees space at once, so the rewrite fits),
runs VACUUM FULL (rewrites the live rows; the primary key is rebuilt with it), then recreates the
dropped indexes from their recorded definitions and runs ANALYZE. If a step fails, every recorded
index that is missing is recreated before it exits. No row is deleted. Each table is locked while
it is rewritten (seconds to a minute); the website keeps serving its CDN copies meanwhile.

Run from services/api (it reads NEON_DB from the repo's .env, direct host, never the pooler):
    uv run python ../../infra/neon_compact.py --dry-run     # what it would do
    uv run python ../../infra/neon_compact.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]
TABLES = ("burn_cluster", "harvest_cluster", "smoke_run")


def neon_url() -> str:
    """NEON_DB from .env, as SQLAlchemy needs it: quotes, a pasted `psql` and '-pooler' removed."""
    load_dotenv(REPO_ROOT / ".env")
    raw = (os.getenv("NEON_DB") or "").strip()
    if raw.startswith("psql "):
        raw = raw[len("psql "):].strip()
    raw = re.sub(r"-pooler(\.)", r"\1", raw.strip("'\"").strip(), count=1)
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql+psycopg://" + raw[len(prefix):]
    if "neon.tech" not in raw:
        sys.exit("NEON_DB in .env must be the Neon connection string")
    return raw


def database_mb(c) -> float:
    return c.execute(text("SELECT pg_database_size(current_database())")).scalar() / 1048576


def secondary_indexes(c, table: str) -> list[tuple[str, str]]:
    """(name, CREATE INDEX statement) of every index on `table` that no constraint owns."""
    return [(n, d) for n, d in c.execute(text("""
        SELECT i.indexname, i.indexdef FROM pg_indexes i
        WHERE i.schemaname = 'public' AND i.tablename = :t
          AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conindid = quote_ident(i.indexname)::regclass)
    """), {"t": table})]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show the plan and sizes, change nothing")
    args = parser.parse_args()

    engine = create_engine(neon_url(), pool_pre_ping=True).execution_options(isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        print(f"start: database {database_mb(c):.1f} MB")
        for table in TABLES:
            indexes = secondary_indexes(c, table)
            live = c.execute(text(f"SELECT coalesce(sum(pg_column_size(t.*)), 0) FROM public.{table} t")).scalar() / 1048576
            size = c.execute(text("SELECT pg_total_relation_size(:t)"), {"t": f"public.{table}"}).scalar() / 1048576
            print(f"== {table}: {size:.1f} MB on disk, {live:.1f} MB of live rows; secondary indexes {[n for n, _ in indexes]}")
            if args.dry_run:
                continue
            try:
                for name, _ in indexes:
                    c.execute(text(f'DROP INDEX IF EXISTS public."{name}"'))
                print(f"   indexes dropped: database {database_mb(c):.1f} MB")
                started = time.time()
                c.execute(text(f"VACUUM FULL public.{table}"))
                print(f"   VACUUM FULL in {time.time() - started:.1f} s: database {database_mb(c):.1f} MB")
            finally:
                present = {n for (n,) in c.execute(text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"), {"t": table})}
                for name, ddl in indexes:
                    if name not in present:
                        started = time.time()
                        c.execute(text(ddl))
                        print(f"   recreated {name} in {time.time() - started:.1f} s")
                c.execute(text(f"ANALYZE public.{table}"))
        print(f"end: database {database_mb(c):.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
