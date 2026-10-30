"""Integration: the headline endpoint, the shared response cache, the serverless import rule and
INIT_DB_ON_STARTUP. Runs against blindspot_test (conftest), whose tables may be empty: the
headline must then answer 200 with honest `available: false` blocks, never invented numbers."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from blindspot.api import cache
from blindspot.api import main as api_main
from blindspot.api.main import app

API_DIR = Path(__file__).resolve().parents[1]

# Not shipped in the serverless (Vercel) build of the web API; see services/api/README.md.
HEAVY = ("rasterio", "geopandas", "pyogrio", "pandas", "scipy", "lightgbm", "h5py", "pyarrow",
         "xarray", "odc", "dask", "pdfplumber", "sklearn", "PIL", "pyproj")  # fmt: skip


def test_api_imports_without_heavy_modules() -> None:
    """The app and every router import with the heavy modules made unimportable."""
    code = (
        "import sys\n"
        f"for name in {HEAVY!r}:\n"
        "    sys.modules[name] = None\n"
        "import importlib, pkgutil\n"
        "from blindspot.api.main import app\n"
        "import blindspot.api.routers as r\n"
        "for m in pkgutil.iter_modules(r.__path__):\n"
        "    importlib.import_module('blindspot.api.routers.' + m.name)\n"
        "assert not app.state.router_errors, app.state.router_errors\n"
        "paths = app.openapi()['paths']\n"
        "assert '/api/stats/headline' in paths and '/api/fires' in paths, sorted(paths)\n"
        "print('OK', len(paths))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=API_DIR, capture_output=True, text=True, timeout=180, check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(API_DIR)},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip().startswith("OK")


def test_headline_contract() -> None:
    cache.clear()
    with TestClient(app) as client:
        r = client.get("/api/stats/headline")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated_at"].endswith("Z")
    assert isinstance(body["sources"], list)
    for block in ("fires_24h", "season", "unseen_burns", "delhi", "passes", "gk2a_evening"):
        assert isinstance(body[block], dict), block
    # The architecture contract's field names.
    assert {"count", "window"} <= set(body["fires_24h"])
    assert {"pm25", "observed_at"} <= set(body["delhi"]) or body["delhi"]["available"] is False
    assert {"last", "next"} <= set(body["passes"]) or body["passes"]["available"] is False
    # Unavailable blocks explain themselves instead of showing numbers.
    for name in ("fires_24h", "unseen_burns", "passes", "gk2a_evening"):
        block = body[name]
        if block.get("available") is False:
            assert block.get("reason"), name
    if body["unseen_burns"].get("available"):
        assert body["unseen_burns"]["label"] == "candidate unseen burns"
        assert 0 < body["unseen_burns"]["precision"] <= 1


def test_get_or_build_single_flight_and_errors_not_cached() -> None:
    cache.clear()
    calls = []

    def slow() -> int:
        calls.append(1)
        time.sleep(0.2)
        return 42

    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_or_build("t", "k", slow, ttl=5)))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [42] * 5
    assert len(calls) == 1  # five simultaneous requests, one build

    def broken() -> int:
        raise RuntimeError("upstream down")

    for _ in range(2):  # a failure is not remembered: the next request tries again
        with pytest.raises(RuntimeError):
            cache.get_or_build("t", "err", broken, ttl=5)


def test_cached_route_replays_200_only() -> None:
    cache.clear()
    counter = {"n": 0}

    @cache.cached_route(ttl=5)
    def route(city: str = "Delhi", status: int = 200) -> Response:
        counter["n"] += 1
        return Response(f"{city}:{counter['n']}".encode(), status_code=status)

    assert route(city="Delhi").body == b"Delhi:1"
    assert route(city="Delhi").body == b"Delhi:1"  # replayed
    assert route(city="Agra").body == b"Agra:2"  # a different query is its own entry
    route(city="X", status=404)
    route(city="X", status=404)
    assert counter["n"] == 4  # errors are rebuilt every time


def test_init_db_on_startup_false_skips_init(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(api_main, "init_db", lambda: calls.append(1))
    monkeypatch.setattr(
        api_main, "get_settings",
        lambda: SimpleNamespace(init_db_on_startup=False, enable_scheduler=False,
                                redact=lambda s: s),
    )  # fmt: skip
    with TestClient(app):
        pass
    assert calls == []
    monkeypatch.setattr(
        api_main, "get_settings",
        lambda: SimpleNamespace(init_db_on_startup=True, enable_scheduler=False,
                                redact=lambda s: s),
    )  # fmt: skip
    with TestClient(app):
        pass
    assert calls == [1]


def test_missing_optional_module_is_a_503() -> None:
    """A route needing a library this deployment lacks answers 503 with the module's name."""
    from fastapi import APIRouter

    probe = APIRouter()

    @probe.get("/api/_probe_missing_module")
    def needs_missing() -> dict:
        import blindspot_module_that_is_not_installed  # noqa: F401

        return {}

    app.include_router(probe)
    try:
        with TestClient(app) as client:
            r = client.get("/api/_probe_missing_module")
        assert r.status_code == 503
        assert r.json()["missing_module"] == "blindspot_module_that_is_not_installed"
    finally:
        app.router.routes[:] = [
            route for route in app.router.routes
            if getattr(route, "path", None) != "/api/_probe_missing_module"
        ]  # fmt: skip
