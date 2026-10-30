"""The FastAPI application.

create_app() builds it: CORS, gzip, orjson responses, JSON errors (sent from inside CORS, so the
browser can read them), the core routes /api/health and /api/sources, and every router module in
blindspot/api/routers/. The lifespan creates missing tables (retrying in the background if the
database is not up yet) and, when ENABLE_SCHEDULER=1, starts the job scheduler inside this
process (run exactly one such process).

    uv run uvicorn blindspot.api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from blindspot import __version__
from blindspot.api import SessionDep
from blindspot.api.responses import ORJSONResponse, respond
from blindspot.api.routers import discover_routers
from blindspot.config import get_settings, setup_logging
from blindspot.db import check_db, init_db
from blindspot.http import FetchError, SourceUnavailable, close_client
from blindspot.provenance import source_status, utcnow

log = logging.getLogger(__name__)

INIT_DB_RETRY_S = 5.0  # first wait before trying init_db() again; it doubles up to a minute


# --- errors: always JSON, one shape: {detail, status, path, generated_at, sources, ...} ----------


def _error_body(status: int, detail: Any, path: str, **extra: Any) -> dict[str, Any]:
    # `sources` too: architecture.md says every JSON response carries generated_at and sources.
    return {
        "detail": detail,
        "status": status,
        "path": path,
        "generated_at": utcnow(),
        "sources": [],
        **extra,
    }


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    headers = getattr(exc, "headers", None)
    if exc.status_code in (204, 304) or exc.status_code < 200:  # these may not carry a body
        return Response(status_code=exc.status_code, headers=headers)
    body = _error_body(exc.status_code, exc.detail, request.url.path)
    return ORJSONResponse(body, status_code=exc.status_code, headers=headers)


async def _validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    body = _error_body(422, jsonable_encoder(exc.errors()), request.url.path)
    return ORJSONResponse(body, status_code=422)


async def _upstream_error(request: Request, exc: Exception) -> Response:
    # An on-demand upstream call failed: 503 while the source is paused, 502 otherwise.
    assert isinstance(exc, FetchError)
    status = 503 if isinstance(exc, SourceUnavailable) else 502
    headers = {"Retry-After": str(int(exc.retry_after_s) + 1)} if exc.retry_after_s else None
    body = _error_body(status, str(exc), request.url.path, source=exc.source_key)
    return ORJSONResponse(body, status_code=status, headers=headers)


async def _database_error(request: Request, exc: Exception) -> Response:
    log.warning("database error on %s: %s", request.url.path, exc)
    body = _error_body(503, "database unavailable", request.url.path)
    return ORJSONResponse(body, status_code=503)


class _JSONServerErrors:
    """Turns any other exception into a JSON 500.

    Starlette sends its own plain-text 500 from outside every middleware, so the browser would
    see a CORS failure instead of the error. This sits inside CORS and answers first.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_and_track(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_and_track)
        except Exception as exc:
            log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"))
            if started:  # part of the response is already out; nothing better can be sent
                raise
            error = get_settings().redact(f"{type(exc).__name__}: {exc}")[:500]
            body = _error_body(500, "internal server error", scope.get("path", ""), error=error)
            await ORJSONResponse(body, status_code=500)(scope, receive, send)


# --- core routes ---------------------------------------------------------------------------------

core = APIRouter(prefix="/api", tags=["core"])


@core.get("/health")
def health(request: Request) -> ORJSONResponse:
    """Is the API up and can it reach its database?"""
    db_ok = check_db()
    scheduler_on = getattr(request.app.state, "scheduler", None) is not None
    return respond(
        {
            "ok": db_ok,
            "time": utcnow(),
            "db": db_ok,
            "version": __version__,
            "scheduler": scheduler_on,
        }
    )


@core.get("/sources")
def sources(session: SessionDep) -> ORJSONResponse:
    """Freshness of every upstream source: ok / stale / error / never, with the last fetch."""
    return respond({"sources": source_status(session)}, max_age=15)


# --- app -----------------------------------------------------------------------------------------


def _first_line(exc: BaseException) -> str:
    message = str(exc).strip()
    return get_settings().redact(message.splitlines()[0] if message else type(exc).__name__)


def init_db_until_ready(stop: threading.Event) -> bool:
    """Retry init_db() after it failed at start-up because the database was not reachable (a fresh
    Docker volume takes a few seconds to accept connections). Without this, a database that came
    up a moment after the API would be left without its tables until the next restart.

    Returns True once the tables are in place, False when `stop` is set (the app is shutting down)
    or the failure is not about reaching the database (then retrying would not help).
    """
    wait = INIT_DB_RETRY_S
    while not stop.wait(wait):
        try:
            init_db()
        except OperationalError as exc:
            wait = min(wait * 2, 60.0)
            log.warning(
                "database still not reachable (%s); next try in %.0f s", _first_line(exc), wait
            )
        except Exception:
            log.exception("database setup failed; not retrying")
            return False
        else:
            log.info("database reachable: tables are in place")
            return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    stop = threading.Event()
    try:
        await run_in_threadpool(init_db)
    except OperationalError as exc:
        log.error(
            "database not reachable (%s); serving anyway (/api/health reports db=false) and "
            "retrying in the background",
            _first_line(exc),
        )
        threading.Thread(
            target=init_db_until_ready, args=(stop,), name="init-db-retry", daemon=True
        ).start()
    except Exception:
        log.exception("database setup failed; serving anyway, and /api/health reports db=false")
    if get_settings().enable_scheduler:
        try:
            from blindspot.jobs import build_scheduler  # only this mode needs the jobs imported

            scheduler = await run_in_threadpool(build_scheduler)
            scheduler.start()
            app.state.scheduler = scheduler
            names = ", ".join(entry.id for entry in scheduler.get_jobs()) or "no jobs"
            log.info("scheduler started: %s", names)
        except Exception:
            log.exception("scheduler failed to start; serving without it")
    try:
        yield
    finally:
        stop.set()
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
            app.state.scheduler = None
        close_client()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()
    app = FastAPI(
        title="BlindSpot API",
        version=__version__,
        lifespan=lifespan,
        default_response_class=ORJSONResponse,
    )
    app.state.scheduler = None

    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(FetchError, _upstream_error)
    app.add_exception_handler(OperationalError, _database_error)
    # The middleware added last runs first: CORS -> gzip -> JSON 500s -> routes.
    app.add_middleware(_JSONServerErrors)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],
        max_age=600,
    )

    app.include_router(core)
    routers, failed = discover_routers()
    for router in routers:
        app.include_router(router)
    app.state.router_errors = failed  # {module: error} for routers that did not import
    log.info("mounted %d router module(s), %d failed to import", len(routers), len(failed))
    return app


app = create_app()
