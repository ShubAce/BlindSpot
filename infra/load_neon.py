"""Load the hosted database (Neon, free tier: 0.5 GB) from the local dev database, compacted.

Run from services/api, with DATABASE_URL set in the environment to Neon's *direct* host (the
NEON_DB value in .env without '-pooler'; never the pooler, never localhost):
    cd services/api && uv run python ../../infra/load_neon.py [--source URL]

Steps (idempotent: run it again any time; the target's app tables are emptied first):
1. create the schema with the app's own init_db() (PostGIS + every table of blindspot.models);
2. TRUNCATE ... RESTART IDENTITY CASCADE every app table on the target;
3. copy each app table with COPY, streamed (a table is never held in memory), applying on the way
   - boundary: polygons simplified exactly as the boundaries job now stores them (sources/
     boundaries.py: ~20 m, India's outline exact), and area_km2 recomputed as that job does;
   - harvest_cluster: risk_factors compacted to {"run", "v"}, plus the risk_run row holding what
     the run shares and its backtest (analytics/risk.py); a row whose compact form would not
     re-hydrate to exactly the stored dict is copied as it is;
   - smoke_run, met_run, met_grid: only the rows the `prune` job keeps (jobs/prune.py);
   tables outside blindspot.models (the PostGIS image's TIGER geocoder, spatial_ref_sys) are not
   copied;
4. reset every id sequence, ANALYZE, and print each table's rows and size and the database size.

The source defaults to the local Docker database. Nothing in the source is written.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

API_DIR = Path(__file__).resolve().parents[1] / "services" / "api"
LOCAL_SOURCE = "postgresql://blindspot:blindspot@localhost:5433/blindspot"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
BATCH = 2000  # rows per round trip when rows are transformed in Python


def target_problem(url: str) -> str | None:
    """Why `url` must not be the target, or None. The loader truncates its tables."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        host = (make_url(url).host or "").lower()
    except (ArgumentError, ValueError):
        return "DATABASE_URL is not a database URL"
    if not host or host in LOCAL_HOSTS:
        return "the target is a local database; this loader only writes to Neon"
    if "-pooler" in host:
        return "the target is Neon's pooler (PgBouncer); use the direct host (remove '-pooler')"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default=LOCAL_SOURCE, help="source database (default: local Docker)")
    args = parser.parse_args()
    target = os.environ.get("DATABASE_URL", "")
    problem = target_problem(target) if target else "DATABASE_URL is not set in the environment"
    if problem:
        print(f"refusing to run: {problem}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(API_DIR))
    import psycopg

    from blindspot.config import setup_logging
    from blindspot.db import Base, engine, init_db

    setup_logging()
    started = time.perf_counter()
    print("target: Neon (direct host)")
    init_db()
    import blindspot.models  # noqa: F401  (every table on Base.metadata)

    with psycopg.connect(args.source) as src, engine.connect() as conn:
        src.execute("SET TIME ZONE 'UTC'")
        dst = conn.connection.driver_connection  # psycopg 3 connection to the target
        dst.execute("SET TIME ZONE 'UTC'")
        source_tables = {r[0] for r in src.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        tables = [t.name for t in Base.metadata.sorted_tables]  # parents before children
        tables.remove("risk_run")  # before harvest_cluster, whose rows point at it
        tables.insert(tables.index("harvest_cluster"), "risk_run")
        keep = _keep_filters()
        dst.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        report: dict[str, dict[str, Any]] = {}
        for table in tables:
            t0 = time.perf_counter()
            if table not in source_tables:
                report[table] = {"note": "not in the source"}
                continue
            cols = _common_columns(src, dst, table)
            if table == "boundary":
                note = copy_boundaries(src, dst, cols)
            elif table == "harvest_cluster":
                note = copy_harvest(src, dst, cols)
            else:
                note = copy_table(src, dst, table, cols, keep.get(table))
            dst.commit()
            report[table] = {"note": note, "seconds": round(time.perf_counter() - t0, 1)}
            print(f"  {table}: {note} ({report[table]['seconds']} s)", flush=True)
        _reset_sequences(dst, tables)
        dst.commit()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as auto:
        auto.exec_driver_sql("ANALYZE")
    _print_sizes(tables, report)
    print(f"done in {time.perf_counter() - started:.0f} s")
    return 0


# --- plain tables ---------------------------------------------------------------------------------


def _keep_filters() -> dict[str, str]:
    """WHERE clauses (on the source) for the tables the prune job trims."""
    from blindspot.jobs.prune import KEEP_MET_RUNS, KEEP_SMOKE_RUNS

    return {
        "smoke_run": f"id IN ({KEEP_SMOKE_RUNS})",
        "met_run": f"id IN ({KEEP_MET_RUNS})",
        "met_grid": f"run_id IN ({KEEP_MET_RUNS})",
    }


def _common_columns(src: Any, dst: Any, table: str) -> list[str]:
    """The target's columns that the source also has, in the target's order."""
    sql = ("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
           "AND table_name = %s ORDER BY ordinal_position")
    have = {r[0] for r in src.execute(sql, (table,))}
    return [r[0] for r in dst.execute(sql, (table,)) if r[0] in have]


def copy_table(src: Any, dst: Any, table: str, cols: list[str], where: str | None) -> str:
    """COPY the source rows straight into the target, block by block."""
    names = ", ".join(f'"{c}"' for c in cols)
    select = f"SELECT {names} FROM {table}" + (f" WHERE {where}" if where else "")
    with src.cursor().copy(f"COPY ({select}) TO STDOUT") as out, \
            dst.cursor().copy(f"COPY {table} ({names}) FROM STDIN") as into:
        for block in out:
            into.write(block)
    copied = dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    total = src.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return f"{copied:,} rows" + (f" of {total:,} (kept by the prune rules)" if where else "")


# --- boundary: simplified polygons ----------------------------------------------------------------


def copy_boundaries(src: Any, dst: Any, cols: list[str]) -> str:
    """Boundary rows with their polygons simplified as the boundaries job stores them.

    The rows go through a staging table so the polygons are made valid and typed by the same SQL
    the job uses (ST_MakeValid, polygons only, MultiPolygon), and area_km2 is then recomputed by the
    job's own statement."""
    import shapely

    from blindspot.config import get_settings
    from blindspot.jobs.boundaries import _SET_AREA_KM2, AREA_SOURCES
    from blindspot.sources import boundaries as srcmod

    states = [shapely.from_wkb(bytes(r[0])) for r in src.execute(
        "SELECT ST_AsBinary(geom) FROM boundary WHERE level = 'state'")]
    band = srcmod.outline_band(states, get_settings().met_bbox)
    del states
    other = [c for c in cols if c != "geom"]
    names = ", ".join(f'"{c}"' for c in other)
    dst.execute(f"CREATE TEMP TABLE boundary_stage AS SELECT {names} FROM boundary WITH NO DATA")
    dst.execute("ALTER TABLE boundary_stage ADD COLUMN wkb bytea")
    before = after = 0
    with src.cursor(name="boundary_rows") as cur, \
            dst.cursor().copy(f"COPY boundary_stage ({names}, wkb) FROM STDIN") as into:
        cur.itersize = 20  # a state polygon is up to a few MB
        cur.execute(f"SELECT {names}, ST_AsBinary(geom) FROM boundary ORDER BY id")
        for row in cur:
            geom = shapely.from_wkb(bytes(row[-1]))
            simple = srcmod.simplify_area(geom, band)
            before += shapely.get_num_coordinates(geom)
            after += shapely.get_num_coordinates(simple)
            into.write_row((*(_copy_value(v) for v in row[:-1]), shapely.to_wkb(simple)))
    dst.execute(f"""
        INSERT INTO boundary ({names}, geom)
        SELECT {names}, ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(wkb, 4326)), 3))
        FROM boundary_stage""")
    dst.execute("DROP TABLE boundary_stage")
    with dst.cursor() as cur:
        cur.execute(str(_SET_AREA_KM2.text).replace(":s", "%(s)s"), {"s": AREA_SOURCES})
    n = dst.execute("SELECT count(*) FROM boundary").fetchone()[0]
    return f"{n:,} rows, {before:,} -> {after:,} vertices"


def _copy_value(value: Any) -> Any:
    """psycopg dumps dicts and lists as JSON only when told; boundary.props is the one jsonb here."""
    if isinstance(value, (dict, list)):
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    return value


# --- harvest_cluster: compact risk factors --------------------------------------------------------


def copy_harvest(src: Any, dst: Any, cols: list[str]) -> str:
    """harvest_cluster with risk_factors stored compactly, and the risk_run row they point at.

    Rows scored by the old job carry the full factors dict, all with the same as_of (the job scores
    every field in one run). That run becomes a risk_run row (its shared values, and the backtest
    report the job left on disk); each row keeps only its own numbers. A row is compacted only if
    expanding the compact form gives back exactly the stored dict; otherwise it is copied as is,
    and readers still understand it."""
    import orjson
    from psycopg.types.json import Jsonb

    from blindspot.analytics import risk as model
    from blindspot.config import get_settings

    legacy_as_of = src.execute(
        "SELECT max(risk_factors ->> 'as_of') FROM harvest_cluster "
        "WHERE risk_factors IS NOT NULL AND NOT risk_factors ? 'v'").fetchone()[0]
    newer_run = src.execute(
        "SELECT max(as_of) FROM risk_run WHERE as_of > %s::timestamptz", (legacy_as_of,)
    ).fetchone()[0] if legacy_as_of and _has_table(src, "risk_run") else None
    run_id = shared = None
    if legacy_as_of and newer_run is None:
        sample = src.execute("SELECT risk_factors::text FROM harvest_cluster WHERE risk_factors ->> 'as_of' = %s "
                             "LIMIT 1", (legacy_as_of,)).fetchone()[0]
        shared = orjson.loads(orjson.dumps(model.run_constants(orjson.loads(sample))))
        try:
            backtest = orjson.loads(get_settings().cache_path(*model.BACKTEST_FILE).read_bytes())
        except (OSError, ValueError):
            backtest = None
        fields = src.execute("SELECT count(*) FROM harvest_cluster WHERE risk_factors ->> 'as_of' = %s",
                             (legacy_as_of,)).fetchone()[0]
        run_id = dst.execute(
            "INSERT INTO risk_run (id, as_of, model, fields, changed, factors, backtest) "
            "VALUES ((SELECT coalesce(max(id), 0) + 1 FROM risk_run), %s::timestamptz, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (legacy_as_of, shared.get("model"), fields, fields, Jsonb(shared),
             Jsonb(backtest) if backtest else None)).fetchone()[0]
    names = ", ".join(f'"{c}"' for c in cols)
    i_factors, i_after, i_before = cols.index("risk_factors"), cols.index("harvested_after"), cols.index("harvested_before")
    select = ", ".join("risk_factors::text" if c == "risk_factors" else f'"{c}"' for c in cols)
    counts = {"compact": 0, "kept_full": 0, "unscored": 0}
    with src.cursor(name="harvest_rows") as cur, \
            dst.cursor().copy(f"COPY harvest_cluster ({names}) FROM STDIN") as into:
        cur.itersize = BATCH
        cur.execute(f"SELECT {select} FROM harvest_cluster ORDER BY id")
        for row in cur:
            row = list(row)
            stored = row[i_factors]
            if stored is None:
                counts["unscored"] += 1
            else:
                factors = orjson.loads(stored)
                compact = None
                if run_id is not None and not model.is_compact(factors) and factors.get("as_of") == shared["as_of"] \
                        and orjson.dumps(model.run_constants(factors)) == orjson.dumps(shared):
                    compact = {"run": run_id, "v": model.pack_values(model.row_values(factors))}
                    back = model.expand_factors(compact, shared, row[i_after], row[i_before])
                    if orjson.dumps(back) != orjson.dumps(factors):
                        compact = None
                if compact is not None:
                    row[i_factors] = orjson.dumps(compact).decode()
                    counts["compact"] += 1
                else:
                    counts["kept_full"] += 1
            into.write_row(row)
    return (f"{sum(counts.values()):,} rows: {counts['compact']:,} compacted, {counts['kept_full']:,} copied as "
            f"stored, {counts['unscored']:,} unscored" + (f"; risk_run {run_id} added" if run_id else ""))


def _has_table(conn: Any, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)).fetchone()[0]


# --- finishing ------------------------------------------------------------------------------------


def _reset_sequences(dst: Any, tables: list[str]) -> None:
    """Point every id sequence after the largest id copied (COPY wrote explicit ids)."""
    owned = dst.execute("""
        SELECT s.oid::regclass::text, t.relname, a.attname
        FROM pg_class s
        JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
        WHERE s.relkind = 'S' AND t.relname = ANY(%s)
    """, (tables,)).fetchall()
    for seq, table, column in owned:
        dst.execute(f'SELECT setval(%s, coalesce((SELECT max("{column}") FROM {table}), 0) + 1, false)', (seq,))


def _print_sizes(tables: list[str], report: dict[str, dict[str, Any]]) -> None:
    from sqlalchemy import text

    from blindspot.db import engine

    with engine.connect() as conn:
        print(f"\n{'table':<24}{'rows':>12}{'table+toast':>14}{'indexes':>12}{'total':>12}")
        for table in sorted(tables, key=lambda t: -conn.execute(
                text("SELECT pg_total_relation_size(to_regclass(:t))"), {"t": f"public.{t}"}).scalar_one()):
            rows, tbl, idx, total = conn.execute(text(
                "SELECT (SELECT count(*) FROM " + table + "), pg_table_size(to_regclass(:t)), "
                "pg_indexes_size(to_regclass(:t)), pg_total_relation_size(to_regclass(:t))"),
                {"t": f"public.{table}"}).one()
            print(f"{table:<24}{rows:>12,}{_mb(tbl):>14}{_mb(idx):>12}{_mb(total):>12}")
        size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        print(f"\npg_database_size: {_mb(size)}")


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


if __name__ == "__main__":
    sys.exit(main())
