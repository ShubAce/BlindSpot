"""Shared test setup (owned by the foundation; module tests just rely on it).

Tests run against a separate database, <name>_test, on the same local PostGIS server, so they
never touch the data the app serves. It is created on first use, and init_db() fills it with
tables. The job scheduler is always off in tests.
"""

from __future__ import annotations

import os
from contextlib import suppress

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


def _point_at_test_database() -> None:
    # Must run before anything imports blindspot.db, which builds its engine from the settings.
    from blindspot.config import Settings, get_settings

    url = make_url(Settings().database_url)
    name = url.database or "blindspot"
    test_url = url if name.endswith("_test") else url.set(database=f"{name}_test")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": test_url.database}
            ).scalar()
            if not exists:
                quoted = conn.dialect.identifier_preparer.quote(test_url.database)
                with suppress(DBAPIError):  # a parallel test run may have just created it
                    conn.execute(text(f"CREATE DATABASE {quoted}"))
    finally:
        admin.dispose()
    os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    os.environ["ENABLE_SCHEDULER"] = "0"
    get_settings.cache_clear()


_point_at_test_database()
