"""A small in-process cache for hot read endpoints (architecture.md section 8: "API responses
cached briefly in-process (10-60 s)").

Why: the live page asks the same handful of questions (fires in 24 h, air now, the headline) every
few seconds from every open browser, and the answers only change when a job writes new rows (every
30 min at best). Answering identical requests from memory for half a minute keeps the database
idle and the latency flat during a demo, without serving anything older than the TTL.

Two ways to use it:

    from blindspot.api.cache import cached_route, get_or_build

    @router.get("/latest")
    @cached_route(ttl=30)                       # caches the finished 200 response per query
    def latest(session: SessionDep, city: str = "Delhi") -> ORJSONResponse: ...

    payload = get_or_build("headline", key, build, ttl=60)   # cache any value

Each key is built once even when many requests arrive together (single flight): the others wait
for the first and get its result. Errors are never cached. The cache is per process, so on
serverless each warm instance has its own; that is fine for a 30 s TTL.

CDNCacheHeaders (end of this file) adds `s-maxage` so that Vercel's CDN, which sits in front of
every serverless instance, answers repeat requests without waking a function at all.
"""

from __future__ import annotations

import functools
import inspect
import re
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from cachetools import TTLCache
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_caches: dict[tuple[str, float], TTLCache[Hashable, Any]] = {}
_key_locks: dict[tuple[str, Hashable], threading.Lock] = {}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0}


def _cache_for(name: str, ttl: float, maxsize: int) -> TTLCache[Hashable, Any]:
    with _lock:
        cache = _caches.get((name, ttl))
        if cache is None:
            cache = _caches[(name, ttl)] = TTLCache(maxsize=maxsize, ttl=ttl)
        return cache


def get_or_build[T](
    name: str, key: Hashable, build: Callable[[], T], ttl: float = 30, maxsize: int = 128
) -> T:
    """The cached value for (name, key), or build() it, store it for `ttl` seconds and return it.

    Concurrent callers with the same key wait for one build instead of each querying the database.
    If build() raises, nothing is stored and the exception reaches the caller.
    """
    cache = _cache_for(name, ttl, maxsize)
    with _lock:
        if key in cache:
            _stats["hits"] += 1
            return cache[key]
        key_lock = _key_locks.setdefault((name, key), threading.Lock())
    with key_lock:
        with _lock:  # another request may have built it while we waited
            if key in cache:
                _stats["hits"] += 1
                return cache[key]
        value = build()
        with _lock:
            cache[key] = value
            _stats["misses"] += 1
            _key_locks.pop((name, key), None)
        return value


def clear() -> None:
    """Forget everything (tests; or after a job wrote rows and the next answer must be fresh)."""
    with _lock:
        for cache in _caches.values():
            cache.clear()


def stats() -> dict[str, int]:
    with _lock:
        return {**_stats, "entries": sum(len(c) for c in _caches.values())}


# --- whole responses ------------------------------------------------------------------------------


def _freeze(value: Any) -> Hashable:
    """A hashable stand-in for a route argument (query values are str/int/float/date/enums)."""
    if isinstance(value, (str, int, float, bool, type(None), date, datetime, Enum)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return repr(value)


class _Snapshot:
    """What is needed to send the same response again: status, body bytes and headers."""

    __slots__ = ("body", "headers", "media_type", "status_code")

    def __init__(self, response: Response) -> None:
        self.status_code = response.status_code
        self.body = bytes(response.body)
        self.media_type = response.media_type
        # content-length is recomputed by Response; every other header is kept as sent.
        self.headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}

    def replay(self) -> Response:
        return Response(
            self.body, status_code=self.status_code, headers=self.headers, media_type=self.media_type
        )


def cached_route(ttl: float = 30, maxsize: int = 64) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Cache a route's 200 responses for `ttl` seconds, keyed by its query/path arguments.

    The session and the request object are not part of the key. The cached body keeps the
    `generated_at` of the moment it was built, which is the truth about the data's age. Only
    Response objects with status 200 are stored; errors and anything else pass straight through.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)
        name = f"route:{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind_partial(*args, **kwargs)
            key = tuple(
                (k, _freeze(v))
                for k, v in sorted(bound.arguments.items())
                if not isinstance(v, (Session, Request))
            )
            cache = _cache_for(name, ttl, maxsize)
            with _lock:
                hit = cache.get(key)
                if hit is not None:
                    _stats["hits"] += 1
            if hit is not None:
                return hit.replay()
            response = fn(*args, **kwargs)
            if isinstance(response, Response) and response.status_code == 200 and hasattr(
                response, "body"
            ):
                with _lock:
                    cache[key] = _Snapshot(response)
                    _stats["misses"] += 1
            return response

        return wrapper

    return decorate


# --- CDN headers ----------------------------------------------------------------------------------
# On Vercel the function sits behind a CDN that caches a response only when Cache-Control carries
# `s-maxage` (max-age alone is for browsers). These lifetimes follow the cadence column of
# docs/api-contract-notes.md ("Latency and size"): data that changes every 30 min is cached for a
# minute, slow data (orbits, bulletins, featured frames) for 5 min, geography for an hour. A
# rendered Sentinel-2 picture never changes, so it is immutable.

LIVE_S, SLOW_S, GEOGRAPHY_S = 60, 300, 3600
IMMUTABLE = "public, max-age=31536000, s-maxage=31536000, immutable"
# How long the CDN may keep serving the old answer while it fetches a new one in the background.
STALE_WHILE_REVALIDATE_S = {LIVE_S: 600, SLOW_S: 3600, GEOGRAPHY_S: 86400}


@dataclass(frozen=True)
class CDNRule:
    pattern: re.Pattern[str]
    ttl: int | None  # s-maxage; None: never cached by the CDN
    immutable: bool = False
    finished_runs_only: bool = False  # agent runs: cache only once the run has finished


def _rule(pattern: str, ttl: int | None, **flags: bool) -> CDNRule:
    return CDNRule(re.compile(pattern), ttl, **flags)


# First match wins. Paths that match nothing (the docs, /openapi.json) get no CDN header.
CDN_RULES: tuple[CDNRule, ...] = (
    _rule(r"^/api/health$", None),  # a liveness probe must reach the function
    _rule(r"^/api/agent/(ask|status)\b", None),  # the SSE stream; the live quota
    _rule(r"^/api/agent/runs/", LIVE_S, finished_runs_only=True),
    _rule(r"^/api/imagery/(render|tiles/)", None, immutable=True),
    _rule(r"^/api/imagery/tilejson/", GEOGRAPHY_S),
    _rule(r"^/api/imagery/(featured|scenes)\b", SLOW_S),
    _rule(r"^/api/geo/", GEOGRAPHY_S),
    _rule(r"^/api/orbits/(tle|clock|validation)\b", SLOW_S),
    _rule(r"^/api/air/stubble-share\b", SLOW_S),
    _rule(r"^/api/smoke/validation\b", SLOW_S),
    _rule(r"^/api/bulletin/", SLOW_S),
    _rule(r"^/api/", LIVE_S),  # fires, stats, sources, scars, risk, air, met, smoke, GK-2A, agent
)


def cdn_rule(path: str) -> CDNRule | None:
    return next((rule for rule in CDN_RULES if rule.pattern.match(path)), None)


_MAX_AGE = re.compile(r"(?:^|,)\s*max-age=(\d+)")


def cdn_cache_control(rule: CDNRule, current: str | None) -> str | None:
    """The Cache-Control for a 200 answer under `rule`, or None to leave the response as it is.

    The browser keeps the route's own max-age (the contract's numbers), capped by the CDN lifetime;
    a route that set `no-store`/`private` is never made public."""
    if rule.immutable:
        return IMMUTABLE
    if rule.ttl is None:
        return None
    if current and re.search(r"no-store|no-cache|private", current):
        return None
    found = _MAX_AGE.search(current or "")
    short = min(int(found.group(1)), rule.ttl) if found else min(rule.ttl, LIVE_S)
    swr = STALE_WHILE_REVALIDATE_S.get(rule.ttl, rule.ttl * 10)
    return f"public, max-age={short}, s-maxage={rule.ttl}, stale-while-revalidate={swr}"


class CDNCacheHeaders:
    """ASGI middleware: sets the CDN Cache-Control (see CDN_RULES) on successful GET answers.

    Errors, other methods, the agent's SSE stream and quota, and agent runs still in progress keep
    the headers their route set. `Vary: Origin` is added to every CDN-cached answer, because the
    CORS headers differ per origin and a CDN must not hand one site's answer to another.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rule = cdn_rule(scope.get("path", "")) if scope["type"] == "http" else None
        if rule is None or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        if rule.finished_runs_only:
            await self._after_body(rule, scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                _apply(rule, message)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def _after_body(self, rule: CDNRule, scope: Scope, receive: Receive, send: Send) -> None:
        # GET /api/agent/runs/{id} is ~25 KB: hold it until the body shows whether the run is done.
        start: Message | None = None
        chunks: list[bytes] = []

        async def hold(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            if start["status"] == 200:
                if b'"finished_at":null' in body:  # still running: nobody may keep this answer
                    MutableHeaders(scope=start)["Cache-Control"] = "no-store"
                else:
                    _apply(rule, start)
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, hold)


def _apply(rule: CDNRule, start: Message) -> None:
    headers = MutableHeaders(scope=start)
    value = cdn_cache_control(rule, headers.get("cache-control"))
    if value is not None:
        headers["Cache-Control"] = value
        vary = {v.strip().lower() for v in (headers.get("vary") or "").split(",")}
        if "origin" not in vary:  # CORSMiddleware adds it when the request had an Origin
            headers.add_vary_header("Origin")
