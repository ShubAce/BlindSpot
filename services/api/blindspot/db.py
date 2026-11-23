"""Database plumbing: the engine, sessions, the declarative Base and init_db().

There is one engine per process. Code that writes uses `with session_scope() as session:` (commit
on success, rollback on error); FastAPI routes take `session: Session = Depends(get_session)`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from itertools import pairwise
from typing import Any

import orjson
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from blindspot.config import get_settings

log = logging.getLogger(__name__)

# Any constant works; it only has to be the same in every process that runs init_db().
_INIT_LOCK_KEY = 0x426C5370


class Base(DeclarativeBase):
    """Parent of every table. Tables live in blindspot/models/, one file per module."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


def _json_dumps(value: Any) -> str:
    # JSONB columns accept numpy numbers, datetimes (written as UTC with Z) and Decimals.
    options = (
        orjson.OPT_SERIALIZE_NUMPY
        | orjson.OPT_NON_STR_KEYS
        | orjson.OPT_UTC_Z
        | orjson.OPT_NAIVE_UTC
    )
    return orjson.dumps(value, option=options, default=_json_default).decode()


engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,  # test a pooled connection before use, so a database restart is survived
    pool_size=5,
    max_overflow=10,
    connect_args={"connect_timeout": 5, "application_name": "blindspot"},
    json_serializer=_json_dumps,
    json_deserializer=orjson.loads,
)


@event.listens_for(engine, "connect")
def _use_utc(dbapi_connection: Any, _record: Any) -> None:
    # Timestamps come back in the session time zone. Make it UTC so every datetime we serve is UTC.
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")
    dbapi_connection.commit()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits when the block succeeds and rolls back when it raises."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def vacuum(table: str, analyze: bool = True) -> None:
    """VACUUM (with ANALYZE unless told not to) one table, outside any transaction: the space of its
    old row versions is reused at once instead of whenever autovacuum gets to it, and no lock blocks
    the readers. Never VACUUM FULL from a job: it locks the table and needs a second copy of it on
    disk (giving space back to Neon is done by hand, infra/neon_compact.py)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"VACUUM {'(ANALYZE) ' if analyze else ''}{table}"))


def vacuum_quietly(table: str, analyze: bool = True) -> bool:
    """vacuum(); False instead of an exception, because space housekeeping never fails a job."""
    try:
        vacuum(table, analyze)
        return True
    except SQLAlchemyError as exc:
        log.warning("VACUUM %s failed: %s", table, exc)
        return False


# Neon's free plan stops every write at 1 GB, and a write that touches most of a big table in one
# transaction needs room for a second copy of it. Such writes go in steps of STEP_ROWS rows, each
# its own transaction (id_ranges), with a VACUUM whenever VACUUM_EVERY dead rows have piled up
# (DeadRows), so each step reuses the room of the old row versions before it.
STEP_ROWS = 25_000
VACUUM_EVERY = 20_000
MAX_ID = 2**63 - 1  # above every bigint id: the open end of the last id range


def id_ranges(session: Session, table: str, rows: int = STEP_ROWS) -> list[tuple[int, int]]:
    """[lo, hi) id ranges that cover every id of `table` (and any id outside it), each holding
    about `rows` of its current rows."""
    starts = session.execute(text(
        f"SELECT id FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn FROM {table}) t "
        "WHERE (rn - 1) % :rows = 0 ORDER BY id"), {"rows": max(1, rows)}).scalars().all()
    return list(pairwise([0, *(int(s) for s in starts[1:]), MAX_ID]))


class DeadRows:
    """Rows of one table that a job has rewritten or deleted since its last VACUUM. Each time
    VACUUM_EVERY of them have piled up it runs a plain VACUUM, so the next writes reuse their room
    instead of growing the file; finish() vacuums the rest and refreshes the statistics, once."""

    def __init__(self, table: str) -> None:
        self.table = table
        self.pending = 0
        self.vacuums = 0
        self.written = False

    def add(self, rows: int) -> None:
        self.pending += rows
        self.written = self.written or rows > 0
        if self.pending >= VACUUM_EVERY:
            self._vacuum(analyze=False)

    def finish(self) -> None:
        if self.written:
            self._vacuum(analyze=True)

    def _vacuum(self, analyze: bool) -> None:
        self.vacuums += vacuum_quietly(self.table, analyze)
        self.pending = 0


def get_session() -> Iterator[Session]:
    """FastAPI dependency for routes. It does not commit; a route that writes calls commit()."""
    with SessionLocal() as session:
        yield session


def table_exists(session: Session, *names: str) -> bool:
    """True when every named table exists in the public schema. to_regclass gives NULL for a
    missing table instead of an error, so asking never aborts the session's transaction."""
    return bool(
        session.execute(
            text(
                "SELECT NOT EXISTS (SELECT 1 FROM unnest(CAST(:names AS text[])) AS t(name) "
                "WHERE to_regclass('public.' || name) IS NULL)"
            ),
            {"names": list(names)},
        ).scalar_one()
    )


def init_db() -> None:
    """Create the PostGIS extension and every missing table. Safe to run any number of times.

    create_all only adds missing tables and never alters an existing one. A module that changes
    its columns during development drops its own table in the dev database and runs this again.
    """
    import blindspot.models  # noqa: F401  (importing the package registers every table on Base)

    with engine.begin() as conn:
        # The API and a CLI job may start at the same moment; the lock stops them racing.
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_LOCK_KEY})
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        # Our tables live in `public`. The PostGIS Docker image also installs the TIGER geocoder,
        # whose `tiger` schema is on the search path with its own place, state and county tables:
        # create_all saw tiger.place, decided `place` existed and skipped ours. So, for this
        # transaction only, look in public (plus the schema PostGIS lives in, for its types).
        postgis_schema = conn.execute(
            text(
                "SELECT extnamespace::regnamespace::text FROM pg_extension"
                " WHERE extname = 'postgis'"
            )
        ).scalar_one()
        search_path = ", ".join(dict.fromkeys(["public", postgis_schema]))
        conn.execute(text("SELECT set_config('search_path', :path, true)"), {"path": search_path})
        Base.metadata.create_all(conn)


def check_db() -> bool:
    """True when the database answers a trivial query (used by /api/health)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("database check failed: %s", exc)
        return False
