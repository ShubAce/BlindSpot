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
"""

from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import Callable, Hashable
from datetime import date, datetime
from enum import Enum
from typing import Any

from cachetools import TTLCache
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

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
