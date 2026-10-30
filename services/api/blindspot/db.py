"""Database plumbing: the engine, sessions, the declarative Base and init_db().

There is one engine per process. Code that writes uses `with session_scope() as session:` (commit
on success, rollback on error); FastAPI routes take `session: Session = Depends(get_session)`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import orjson
from sqlalchemy import create_engine, event, text
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


def get_session() -> Iterator[Session]:
    """FastAPI dependency for routes. It does not commit; a route that writes calls commit()."""
    with SessionLocal() as session:
        yield session


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
