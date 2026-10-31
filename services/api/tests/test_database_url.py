"""DATABASE_URL is accepted in the forms people paste into a secret store."""

import pytest

from blindspot.config import Settings

DIRECT = "postgresql+psycopg://user:pw@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"


@pytest.mark.parametrize(
    "pasted",
    [
        "postgresql://user:pw@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
        "postgres://user:pw@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
        "'postgresql://user:pw@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'",
        '"postgresql://user:pw@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"\n',
        "psql 'postgresql://user:pw@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'",
        "postgresql://user:pw@ep-cool-name-123456-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
        "  psql 'postgresql://user:pw@ep-cool-name-123456-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'  ",
    ],
)
def test_pasted_neon_urls_become_the_direct_psycopg_url(pasted):
    assert Settings(database_url=pasted).database_url == DIRECT


def test_local_url_is_unchanged():
    local = "postgresql+psycopg://blindspot:blindspot@localhost:5433/blindspot"
    assert Settings(database_url=local).database_url == local
