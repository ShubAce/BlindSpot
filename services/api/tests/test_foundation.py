"""Foundation tests.

They use the real local PostGIS (a separate *_test database, see conftest.py) and one real,
keyless upstream: Open-Meteo. Retry and cooldown behaviour is exercised against a small local
HTTP server that answers 503 / 429, because a real outage cannot be summoned on demand.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import numpy as np
import orjson
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from blindspot import __version__
from blindspot import http as bs_http
from blindspot.api.responses import respond
from blindspot.config import Settings, get_settings
from blindspot.db import SessionLocal, engine, init_db
from blindspot.geo import (
    AOI_BBOX,
    admin_for_points,
    district_for_point,
    fill_admin,
    nearest_place,
    parse_bbox,
)
from blindspot.http import FetchError, FetchResult, SourceUnavailable, fetch, set_fetch_records
from blindspot.jobs import JOBS, build_scheduler, job, run_job
from blindspot.models import JobRun, SourceFetch
from blindspot.provenance import (
    SOURCE_REGISTRY,
    SourceInfo,
    SourceRef,
    _status,
    latest_refs,
    ref_for_fetch,
    track_fetch,
)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
LUDHIANA = {"latitude": 30.90, "longitude": 75.85}  # in the middle of the Punjab burn belt
PROBE = "probe-not-a-real-key-7f3a"  # sent as `key=` to prove redaction; Open-Meteo ignores it


@pytest.fixture(scope="module", autouse=True)
def database() -> None:
    init_db()


@pytest.fixture(scope="module")
def open_meteo() -> FetchResult:
    """One real, tiny Open-Meteo forecast request, shared by the tests below."""
    params = {**LUDHIANA, "current": "temperature_2m", "key": PROBE}
    return fetch("openmeteo", OPEN_METEO, params=params)


@contextmanager
def local_server(status: int, retry_after: str) -> Iterator[tuple[str, list[str]]]:
    """A local HTTP server that answers every GET with `status` and a Retry-After header."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b'{"error": "unavailable"}'
            self.send_response(status)
            self.send_header("Retry-After", retry_after)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", hits
    finally:
        server.shutdown()
        server.server_close()
        bs_http.reset_cooldowns()


def test_settings_parse_env_style_values() -> None:
    settings = Settings(
        _env_file=None,
        cors_origins="http://a.example/, http://b.example",
        aoi_bbox="74,29,77,32",
        database_url="postgres://user:pw@db.example:5432/blindspot",
        firms_map_key="  0123456789abcdef  ",
    )
    assert settings.cors_origins == ["http://a.example", "http://b.example"]
    assert settings.aoi_bbox == (74.0, 29.0, 77.0, 32.0)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redact("x/0123456789abcdef/y?k=0123456789abcdef") == "x/***/y?k=***"
    assert "0123456789abcdef" not in repr(settings)


def test_init_db_is_idempotent_and_spatial() -> None:
    init_db()  # the fixture ran it once already; a second run must be a no-op
    with engine.connect() as conn:
        postgis = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='postgis'"))
        assert postgis.scalar()
        tables = set(inspect(conn).get_table_names())
        gist_tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_indexes WHERE indexdef ILIKE '%USING gist%'")
            ).scalars()
        )
    assert {"source_fetch", "boundary", "place", "job_run"} <= tables
    assert {"boundary", "place"} <= gist_tables


def test_fetch_records_a_redacted_source_fetch_row(open_meteo: FetchResult) -> None:
    result = open_meteo
    assert result.status_code == 200
    assert "temperature_2m" in result.data["current"]
    assert result.fetch_id is not None
    assert PROBE not in result.url_logged and "key=***" in result.url_logged
    assert not any(secret in result.url_logged for secret in get_settings().secret_values())

    with SessionLocal() as session:
        row = session.get(SourceFetch, result.fetch_id)
        assert row is not None
        assert (row.source_key, row.ok, row.http_status) == ("openmeteo", True, 200)
        assert row.url == result.url_logged and PROBE not in row.url
        assert row.latency_ms is not None and row.finished_at >= row.started_at
        assert row.meta["attempts"] == 1 and row.meta["method"] == "GET"
        ref = ref_for_fetch(session, result.fetch_id)
        assert ref is not None and ref.fetch_id == result.fetch_id and ref.label

    set_fetch_records(result.fetch_id, 1, {"parsed": "current"})
    with SessionLocal() as session:
        row = session.get(SourceFetch, result.fetch_id)
        assert row is not None and row.records == 1
        assert row.meta["parsed"] == "current" and row.meta["attempts"] == 1  # merged, not replaced

        weather, unknown = latest_refs(session, ["openmeteo", "never_fetched_test_source"])
        assert weather.fetch_id is not None and weather.fetched_at is not None
        assert (unknown.url, unknown.fetched_at, unknown.fetch_id) == (None, None, None)


def test_track_fetch_records_calls_made_outside_fetch() -> None:
    # A plain httpx call stands in for an SDK (pystac-client, google-genai) that cannot use fetch().
    ok_url = f"{OPEN_METEO}?latitude=30.9&longitude=75.85&current=temperature_2m"
    with track_fetch("openmeteo", url=ok_url, meta={"via": "track_fetch"}) as tracker:
        response = httpx.get(ok_url, timeout=30)
        response.raise_for_status()
        tracker.http_status, tracker.records = response.status_code, 1
    bad_url = f"{OPEN_METEO}?latitude=300&longitude=75.85&current=temperature_2m"
    with pytest.raises(httpx.HTTPStatusError), track_fetch("openmeteo", url=bad_url) as failed:
        httpx.get(bad_url, timeout=30).raise_for_status()
    with SessionLocal() as session:
        good = session.get(SourceFetch, tracker.fetch_id)
        bad = session.get(SourceFetch, failed.fetch_id)
        assert good is not None and good.ok and good.records == 1
        assert good.meta == {"via": "track_fetch"}
        assert bad is not None and not bad.ok and "HTTPStatusError" in (bad.error or "")


def test_status_rules() -> None:
    now = datetime.now(UTC)
    hourly = SourceInfo("x", "X", "test", 60, None)
    on_demand = SourceInfo("y", "Y", "test", None, None)
    assert _status(hourly, None, None, now) == "never"
    assert _status(hourly, True, now - timedelta(hours=2), now) == "ok"  # within 3 x cadence
    assert _status(hourly, False, now - timedelta(hours=2), now) == "ok"  # one miss, data fresh
    assert _status(hourly, True, now - timedelta(hours=4), now) == "stale"  # job stopped
    assert _status(hourly, False, now - timedelta(hours=4), now) == "error"
    assert _status(hourly, False, None, now) == "error"
    assert _status(on_demand, True, now - timedelta(days=30), now) == "ok"
    assert _status(on_demand, False, now - timedelta(days=30), now) == "error"


def test_respond_serialises_numpy_decimal_and_utc_times() -> None:
    ist_time = datetime(2026, 10, 30, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    payload = {
        "values": np.array([1.5, 2.0], dtype=np.float32),
        "area_ha": Decimal("12.5"),
        "t": datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
        "t_ist_as_utc": ist_time.astimezone(UTC),
    }
    response = respond(payload, [SourceRef(key="firms", label="NASA FIRMS active fires")])
    body = orjson.loads(response.body)
    assert body["values"] == [1.5, 2.0] and body["area_ha"] == 12.5
    assert body["t"] == "2026-10-30T12:00:00Z" and body["t_ist_as_utc"] == "2026-10-30T12:00:00Z"
    assert body["generated_at"].endswith("Z")
    assert body["sources"] == [
        {"key": "firms", "label": "NASA FIRMS active fires", "url": None, "fetched_at": None,
         "fetch_id": None}
    ]
    assert response.headers.get("cache-control") is None
    assert respond({}, max_age=30).headers["cache-control"] == "public, max-age=30"


def test_upstream_rejection_is_recorded_and_not_retried() -> None:
    params = {"latitude": 300, "longitude": 75.85, "current": "temperature_2m"}
    with pytest.raises(FetchError) as caught:
        fetch("openmeteo", OPEN_METEO, params=params)
    error = caught.value
    assert error.status_code == 400 and not isinstance(error, SourceUnavailable)
    with SessionLocal() as session:
        row = session.get(SourceFetch, error.fetch_id)
        assert row is not None and not row.ok and row.http_status == 400
        assert "Latitude" in (row.error or "")  # Open-Meteo's own reason is kept
        assert row.meta["attempts"] == 1  # a 4xx is not retried


def test_transient_errors_are_retried_then_the_source_is_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bs_http, "BACKOFF_BASE_S", 0.0)
    calls = bs_http.COOLDOWN_AFTER_FAILURES
    with local_server(503, "0") as (url, hits):
        for _ in range(calls):
            with pytest.raises(FetchError) as caught:
                fetch("local_test", url)
            assert caught.value.status_code == 503
        assert len(hits) == calls * bs_http.MAX_ATTEMPTS  # each call tried MAX_ATTEMPTS times
        with pytest.raises(SourceUnavailable):
            fetch("local_test", url)
        assert len(hits) == calls * bs_http.MAX_ATTEMPTS  # paused: nothing more was sent


def test_long_retry_after_pauses_the_source_at_once() -> None:
    with local_server(429, "120") as (url, hits):
        with pytest.raises(FetchError) as caught:
            fetch("local_test_429", url)
        assert caught.value.status_code == 429
        assert caught.value.retry_after_s == pytest.approx(120)
        assert len(hits) == 1  # 120 s is too long to wait inside a call, so no retry
        with pytest.raises(SourceUnavailable) as paused:
            fetch("local_test_429", url)
        assert paused.value.retry_after_s is not None and paused.value.retry_after_s > 100
        assert len(hits) == 1


def test_parse_bbox() -> None:
    assert parse_bbox(None) == parse_bbox(" ") == AOI_BBOX
    assert parse_bbox("74,29,77,32") == (74.0, 29.0, 77.0, 32.0)
    for bad in ("74,29,77", "a,b,c,d", "77,29,74,32", "74,95,77,96"):
        with pytest.raises(ValueError):
            parse_bbox(bad)


def test_geo_lookups_return_none_until_boundaries_are_loaded() -> None:
    with SessionLocal() as session:
        # Empty both tables inside a transaction that is rolled back, so whatever the
        # boundaries module's tests have loaded is left alone.
        session.execute(text("DELETE FROM place"))
        session.execute(text("DELETE FROM boundary"))
        assert district_for_point(session, 75.85, 30.90) is None
        assert admin_for_points(session, [(75.85, 30.90), (76.78, 30.73)]) == [None, None]
        assert admin_for_points(session, []) == []
        assert nearest_place(session, 75.85, 30.90) is None
        assert fill_admin(session, "place") == 0
        session.rollback()


def test_job_registry_runs_chains_and_records() -> None:
    calls: list[str] = []

    @job("test_upstream")
    def upstream() -> dict[str, int]:
        """Trivial upstream job."""
        calls.append("upstream")
        return {"rows": 3}

    @job("test_downstream", after="test_upstream")
    def downstream() -> None:
        calls.append("downstream")

    @job("test_broken")
    def broken() -> None:
        raise RuntimeError("deliberate job failure")

    @job("test_hourly", every_minutes=60)
    def hourly() -> None:
        pass

    @job("test_daily", cron="30 7 * * *")
    def daily() -> None:
        pass

    names = ["test_upstream", "test_downstream", "test_broken", "test_hourly", "test_daily"]
    try:
        result = run_job("test_upstream", trigger="test")
        assert result.ok and result.result == {"rows": 3}
        assert [child.name for child in result.chained] == ["test_downstream"]
        assert result.chained[0].ok and calls == ["upstream", "downstream"]

        failed = run_job("test_broken", trigger="test")  # must return, not raise
        assert not failed.ok and failed.chained == []
        assert failed.error is not None and "deliberate job failure" in failed.error

        with SessionLocal() as session:
            runs = session.scalars(
                select(JobRun).where(JobRun.name.in_(names)).order_by(JobRun.id.desc())
            ).all()
        latest: dict[str, JobRun] = {}
        for run in runs:
            latest.setdefault(run.name, run)
        assert latest["test_upstream"].ok and latest["test_upstream"].result == {"rows": 3}
        assert latest["test_downstream"].trigger == "chain"
        assert not latest["test_broken"].ok and "deliberate" in (latest["test_broken"].error or "")

        scheduler = build_scheduler(load=False)  # built but never started: no threads
        scheduled = {entry.id: entry for entry in scheduler.get_jobs()}
        assert {"test_hourly", "test_daily"} <= set(scheduled)
        assert "test_downstream" not in scheduled and "test_upstream" not in scheduled
        assert str(scheduled["test_daily"].trigger.timezone) == "Asia/Kolkata"
    finally:
        for name in names:
            JOBS.pop(name, None)


def test_api_health_sources_and_json_errors(open_meteo: FetchResult) -> None:
    from blindspot.api.main import create_app

    app = create_app()

    @app.get("/api/_test/boom")
    def boom() -> None:
        raise RuntimeError("deliberate route failure")

    @app.get("/api/_test/paused")
    def paused() -> None:
        raise SourceUnavailable("firms", "paused for a test", retry_after_s=42)

    @app.get("/api/_test/number")
    def number(n: int) -> dict[str, int]:
        return {"n": n}

    origin = {"Origin": "http://localhost:3000"}
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        body = health.json()
        assert body["ok"] is True and body["db"] is True and body["version"] == __version__
        assert body["time"].endswith("Z") and body["generated_at"].endswith("Z")

        listing = client.get("/api/sources", headers=origin)
        assert listing.status_code == 200
        assert listing.headers["access-control-allow-origin"] == "http://localhost:3000"
        entries = {entry["key"]: entry for entry in listing.json()["sources"]}
        assert set(SOURCE_REGISTRY) <= set(entries)
        fields = {
            "key", "label", "kind", "status", "last_success_at", "last_attempt_at", "last_error",
            "records_last", "latency_ms_last", "cadence_minutes", "docs_url",
            "url", "fetched_at", "fetch_id",
        }
        for entry in entries.values():
            assert fields <= set(entry)
            assert entry["status"] in {"ok", "stale", "error", "never"}
        weather = entries["openmeteo"]
        assert weather["status"] == "ok" and weather["fetch_id"] is not None
        assert weather["last_success_at"].endswith("Z")
        if "local_test" in entries:  # recorded by the cooldown test above
            assert entries["local_test"]["kind"] == "unregistered"
            assert entries["local_test"]["status"] == "error"

        missing = client.get("/api/does-not-exist")
        assert missing.status_code == 404 and missing.json()["detail"] == "Not Found"

        invalid = client.get("/api/_test/number?n=abc")
        assert invalid.status_code == 422 and isinstance(invalid.json()["detail"], list)

        crashed = client.get("/api/_test/boom", headers=origin)
        assert crashed.status_code == 500
        assert crashed.json()["detail"] == "internal server error"
        assert "deliberate route failure" in crashed.json()["error"]
        assert crashed.headers["access-control-allow-origin"] == "http://localhost:3000"

        unavailable = client.get("/api/_test/paused")
        assert unavailable.status_code == 503 and unavailable.headers["retry-after"] == "43"
        assert unavailable.json()["source"] == "firms"
