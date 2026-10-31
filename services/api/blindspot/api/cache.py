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
from collections.abc import Callable, Hashable, Iterable, Mapping
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
        _novel_builds.clear()


def stats() -> dict[str, int]:
    with _lock:
        return {**_stats, **_store_stats, "entries": sum(len(c) for c in _caches.values())}


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


# --- stored bodies (Neon egress) ------------------------------------------------------------------
# A cold request on serverless recomputes its answer from Neon. For routes whose rows run to
# megabytes (smoke trails, scar polygons, boundaries) the finished payload is kept in the
# response_body table (blindspot.models.assets), xz-compressed, under a `stamp` = the version of the
# data it was computed from + a hash of the code that renders it. A request whose stamp matches
# reads the stored bytes (10-25 % of the rows it replaces); otherwise it computes live and stores
# the result for the next one. Jobs that change the data can store bodies ahead of any request
# (store_payload), so the first visitor after a job run never pays the live cost.
#
# Byte-identity: respond() serialises {"generated_at": now, **payload, "sources": [...]} with
# orjson. The stored body is orjson(payload); serving splices a fresh generated_at in front and
# the sources behind it, which gives exactly the bytes respond() would send at that moment.
# `sources` are stored with the body when they are part of the data (a smoke run's own sources),
# or recomputed per request (`live_sources`) when they follow the newest fetches.

STORE_CODEC = "xz"
_XZ_PRESET = 6  # 30-45 % smaller than gzip-9 on GeoJSON; 10-85 ms to decompress
_code_hashes: dict[tuple[str, ...], str] = {}
_store_stats = {"stored_hits": 0, "stored_misses": 0, "stored_writes": 0}


def source_text(raw: bytes) -> bytes:
    """Source bytes with line endings normalised to LF (and trailing whitespace on each line
    kept): the API is deployed from a Windows working tree (CRLF, copied byte for byte by
    infra/deploy_vercel_api.py) while the jobs run from a Linux checkout of the same commit (LF).
    Hashing the raw bytes gave the job's pre-stored bodies a stamp the API never matched."""
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def code_version(*modules: Any) -> str:
    """A short hash of the source text of `modules` (module objects or dotted names), plus the
    response serialiser and this file: a deploy that changes how a body is rendered never serves
    a body rendered by the old code. Line endings do not count (source_text), so the same commit
    gives the same hash on Windows and on the Linux job runners."""
    import hashlib
    import importlib
    import sys

    names = tuple(sorted({m if isinstance(m, str) else m.__name__ for m in modules}
                         | {"blindspot.api.responses", __name__}))
    found = _code_hashes.get(names)
    if found is None:
        digest = hashlib.sha256()
        for name in names:
            module = sys.modules.get(name) or importlib.import_module(name)
            path = getattr(module, "__file__", None)
            digest.update(name.encode())
            if path:
                with open(path, "rb") as fh:
                    digest.update(source_text(fh.read()))
        found = _code_hashes[names] = digest.hexdigest()[:12]
    return found


def canonical_key(route: str, **query: Any) -> str:
    """`route?k=v&...` with the arguments that are set, sorted: one key per distinct answer."""
    parts = []
    for k, v in sorted(query.items()):
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (date, datetime)):
            v = v.isoformat()
        parts.append(f"{k}={v}")
    return route + ("?" + "&".join(parts) if parts else "")


def _dumps(value: Any) -> bytes:
    """The exact serialisation respond() uses."""
    import orjson

    from blindspot.api import responses

    return orjson.dumps(value, option=responses._OPTIONS, default=responses._default)


def _compress(raw: bytes) -> bytes:
    import lzma

    return lzma.compress(raw, preset=_XZ_PRESET)


def _decompress(codec: str, data: bytes) -> bytes:
    import lzma

    if codec != STORE_CODEC:
        raise ValueError(f"unknown codec {codec!r}")
    return lzma.decompress(data)


def _storable(payload: Mapping[str, Any]) -> bool:
    # respond() treats these two keys specially; such payloads are always computed live.
    return bool(payload) and "generated_at" not in payload and "sources" not in payload


def store_payload(key: str, stamp: str, payload: Mapping[str, Any], sources: Iterable[Any] | None,
                  max_age: int | None, extra_headers: Mapping[str, str] | None = None) -> bool:
    """Store a finished payload (a job calls this after it changed the data; routes call it after
    a live computation). `sources=None` means the route adds live sources at serve time."""
    from blindspot.models.assets import save_body

    if not _storable(payload):
        return False
    raw = _dumps(payload)
    headers = dict(extra_headers or {})
    if max_age is not None:
        headers.setdefault("Cache-Control", f"public, max-age={max_age}")
    ok = save_body(key, stamp, STORE_CODEC, _compress(raw), len(raw),
                   sources=None if sources is None else _dumps(list(sources)), headers=headers)
    if ok:
        with _lock:
            _store_stats["stored_writes"] += 1
    return ok


def _assemble(raw_payload: bytes, sources_json: bytes) -> bytes:
    from blindspot.provenance import utcnow

    head = b'{"generated_at":' + _dumps(utcnow())
    return head + b"," + raw_payload[1:-1] + b',"sources":' + sources_json + b"}"


# --- novel heavy queries ---------------------------------------------------------------------------
# Query parameters are free (max_trails 1..10000, any simplify, any limit), so a client that varies
# them gets a CDN miss and a live read of megabytes from Neon on every request: ~400 such requests
# would use a month's 5 GB. A route marks a costly query `guard=True`; when its key has never been
# stored (no row under it, whatever its stamp), building it live counts against a per-instance
# allowance, and past it the API answers 429 (never cached) instead of reading Neon. Keys the
# website and the jobs use are stored after their first build, so they are never "novel" again
# (until the prune job drops a key nobody read for 3 days); a new stamp on a known key (new data, a
# deploy) is not novel either. When the store cannot be read, nothing is refused.

# The website needs ~12 costly keys on an empty store (a new database), spread over instances.
NOVEL_LIMIT = 20  # live builds of never-stored costly keys per instance...
NOVEL_WINDOW_S = 600.0  # ...per this many seconds
_novel_builds: list[float] = []


class NovelQueryLimited(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(retry_after_s)
        self.retry_after_s = retry_after_s


def _admit_novel() -> None:
    import time

    now = time.monotonic()
    with _lock:
        while _novel_builds and _novel_builds[0] <= now - NOVEL_WINDOW_S:
            _novel_builds.pop(0)
        if len(_novel_builds) >= NOVEL_LIMIT:
            oldest = _novel_builds[0] if _novel_builds else now
            raise NovelQueryLimited(max(1, int(oldest + NOVEL_WINDOW_S - now) + 1))
        _novel_builds.append(now)


def _novel_refusal(exc: NovelQueryLimited) -> Response:
    from blindspot.api.responses import ORJSONResponse

    return ORJSONResponse(
        {"detail": (f"too many new heavy query variants on this server (at most {NOVEL_LIMIT} in "
                    f"{int(NOVEL_WINDOW_S // 60)} min); the variants the website uses are always "
                    "served. Retry later or use a stored variant.")},
        status_code=429, headers={"Retry-After": str(exc.retry_after_s), "Cache-Control": "no-store"})


def stored_payload(
    session: Session, key: str, stamp: str
) -> tuple[dict[str, Any], bytes | None, bytes] | None:
    """The stored payload under `key` for `stamp`: decoded, its stored sources JSON (None when the
    route adds live sources) and its JSON bytes; None when nothing valid is stored. For routes that
    derive a variant from a stored base answer instead of reading the rows again."""
    import orjson

    from blindspot.models.assets import load_body

    hit = load_body(session, key, stamp)
    if hit is None:
        return None
    try:
        raw = _decompress(hit["codec"], hit["body"])
        payload = orjson.loads(raw)
    except Exception:  # noqa: BLE001  (a damaged row: the caller reads the rows)
        return None
    with _lock:
        _store_stats["stored_hits"] += 1
    return payload, hit["sources"], raw


def stored_response(
    session: Session,
    key: str,
    stamp: str,
    build: Callable[[], tuple[Mapping[str, Any], Iterable[Any], int | None]],
    *,
    live_sources: Callable[[], Iterable[Any]] | None = None,
    store: bool = True,
    guard: bool = False,
) -> Response:
    """The stored answer for `key` when it was computed for `stamp`; otherwise build() it live
    (payload, sources, max_age), send it as respond() would, and store it when `store` is true.

    With `live_sources`, sources are recomputed per request (cheap) and never stored, so a stored
    body survives new fetches of sources whose rows it does not depend on. With `guard`, a key
    that was never stored counts against the novel-query allowance (above) before it is built."""
    from blindspot.api.responses import respond
    from blindspot.models.assets import probe_body

    hit, known = probe_body(session, key, stamp)
    if hit is None and guard and known is False:
        try:
            _admit_novel()
        except NovelQueryLimited as exc:
            return _novel_refusal(exc)
    if hit is not None:
        try:
            raw = _decompress(hit["codec"], hit["body"])
            sources_json = hit["sources"] if live_sources is None else _dumps(list(live_sources()))
            if sources_json is not None:
                with _lock:
                    _store_stats["stored_hits"] += 1
                return Response(_assemble(raw, sources_json), media_type="application/json",
                                headers=hit["headers"] or {})
        except Exception:  # noqa: BLE001  (a damaged row: compute live and replace it)
            pass
    payload, sources, max_age = build()
    sources = list(sources)
    response = respond(payload, sources, max_age=max_age)
    with _lock:
        _store_stats["stored_misses"] += 1
    if store and response.status_code == 200:
        store_payload(key, stamp, payload, None if live_sources is not None else sources, max_age)
    return response


# --- Postgres jsonb text, without asking Postgres -------------------------------------------------
# A job that has just written a jsonb column holds the value in memory. To store the body a route
# would render from that row, it needs the value exactly as the route reads it back: Postgres
# normalises jsonb (keys ordered by length then bytes, ", " and ": " separators, numbers in plain
# decimal notation), and a route that passes `col::text` through (smoke trails) sends those bytes.
# jsonb_text() renders that text locally; the caller checks it against md5(col::text), so a
# mismatch costs one stored body (computed live later), never a wrong one.

_JSON_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _jsonb_string(value: str) -> str:
    out = []
    for ch in value:
        esc = _JSON_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _jsonb_number(value: float) -> str:
    import orjson

    from decimal import Decimal

    written = orjson.dumps(value).decode()  # what the database was sent (db._json_dumps is orjson)
    if "e" not in written and "E" not in written and not written.startswith("-0"):
        return written
    number = Decimal(written)
    text_ = format(number, "f")
    return text_.lstrip("-") if number.is_zero() else text_


def jsonb_text(value: Any) -> str:
    """`value::text` as Postgres renders a jsonb column written from `value` (by db._json_dumps).
    `value` must already be plain JSON types (orjson.loads(db._json_dumps(x)) gives that)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _jsonb_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _jsonb_number(value)
    if isinstance(value, list):
        return "[" + ", ".join(jsonb_text(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode()), k.encode()))
        return "{" + ", ".join(f"{_jsonb_string(k)}: {jsonb_text(value[k])}" for k in keys) + "}"
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def fetch_binary(session: Session, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows of `sql` (psycopg `%(name)s` placeholders), read in Postgres' binary format on the
    session's connection and transaction.

    For queries that return bytea (WKB geometry): the text format sends a bytea hex-encoded, twice
    its size, and every byte counts against Neon's egress allowance. Other columns come back as
    the same Python values as in the text format."""
    raw = session.connection().connection.driver_connection
    with raw.cursor(binary=True) as cur:
        cur.execute(sql, dict(params))
        names = [d.name for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def table_stamp(session: Session, table: str) -> str:
    """A version of a table's rows: its row count, the newest row version (xmin) and the sum of
    all row versions. Any insert or update writes a row version with a newer transaction id, and
    a delete lowers the count and the sum, so the stamp changes with every committed change,
    whoever made it (a job, a test, a hand-run fix). One pass over the table, a few bytes back;
    `table` is a trusted constant."""
    from sqlalchemy import text

    count, newest, total = session.execute(
        text(f"SELECT count(*), coalesce(max(xmin::text::bigint), 0), coalesce(sum(xmin::text::bigint), 0) "
             f"FROM {table}")
    ).one()
    return f"{table}:{count}:{newest}:{total}"


def content_stamp(session: Session, table: str, columns: str, where: str = "TRUE",
                  params: Mapping[str, Any] | None = None, order: str = "id") -> str:
    """A version of what a query answers from: the number of rows `where` selects and an md5 over
    the text of their `columns` (in `order`), computed by Postgres, so ~40 bytes come back.

    Unlike table_stamp it changes only when a row the query reads changes in a column the answer
    shows: a job that rewrites a bookkeeping column (sentinel's `matched_at`) or adds
    rows outside the query's window (newer scars, for a replay day) leaves it as it was. `table`,
    `columns`, `where` and `order` are trusted constants; values go in `params`."""
    from sqlalchemy import text

    count, digest = session.execute(
        text(f"SELECT count(*), coalesce(md5(string_agg(md5(ROW({columns})::text), '' ORDER BY {order})), '-') "
             f"FROM {table} WHERE {where}"), dict(params or {})
    ).one()
    return f"{table}:{count}:{digest}"


def job_stamp(session: Session, jobs: Iterable[str]) -> str:
    """When the latest run of any of `jobs` started (job_run), as text: part of a data stamp, so a
    job that rewrote rows in place (labels, flags) invalidates the bodies built from them."""
    from sqlalchemy import text

    value = session.execute(
        text("SELECT max(started_at) FROM job_run WHERE name = ANY(:names)"), {"names": list(jobs)}
    ).scalar()
    return "-" if value is None else value.isoformat()


# --- CDN headers ----------------------------------------------------------------------------------
# On Vercel the function sits behind a CDN that caches a response only when Cache-Control carries
# `s-maxage` (max-age alone is for browsers). Every CDN miss or revalidation is a cold request that
# reads Neon: egress, and it wakes a suspended compute for at least 5 minutes (CU-hours). Neon's
# free plan suspends the database for the rest of the month when either runs out, so the CDN keeps
# each answer until its data can next have changed, and no longer (docs/neon-budget.md).
#
# Expiry is on a clock, not a fixed lifetime: each route belongs to a group whose answers all
# expire at the same moments, shortly after the job that feeds the group is scheduled to have
# finished (.github/workflows/data-jobs.yml; times UTC). A fixed 30-minute lifetime gives every
# route its own phase, so a visitor every few minutes wakes the database every few minutes; on a
# shared clock one page view refreshes the whole group in one wake, and nothing is refreshed
# while the data cannot have changed. Each group has a second point an hour after the first,
# because GitHub starts scheduled workflows late by up to tens of minutes.
#
#   HOURLY      fires, air, GK-2A, the headline, sources, orbit passes: jobs at :17 past hours
#               8-18 and 0, 3, 6, 21; ready by :30.
#   SIX_HOURLY  met grids and smoke runs (met 03:17, 09:17, 15:17, 21:17, smoke chained), IITM DSS,
#               orbit validation: :30 of the next hour (04:30, 10:30, 16:30, 22:30), then 06:30,
#               11:30, 17:30, 00:30 for a late start.
#   DAILY       Sentinel-2 scars, burn risk, featured frames, smoke validation, element sets
#               (11:17, up to 150 min): 13:30 and 14:30.
#   BULLETIN    the daily bulletin (02:00, retried 03:17 and 06:17): :30 of hours 2, 3, 6 (+1 h).
#   WEEKLY      boundaries and places (Monday 03:17): every day at 03:30, so a deploy that changes
#               how they render is never more than a day away.
# Every point but the bulletin's 02:30 is also an HOURLY point, so a page view refreshes every
# group that expired in one database wake-up: at most 21 refresh wake-ups a day (docs/neon-budget.md).
# A rendered Sentinel-2 picture never changes, so it is immutable.
# `stale-while-revalidate` lets the CDN answer at once with the old copy while it fetches a new
# one (the body's generated_at and sources say how old it is); `stale-if-error` lets it keep
# answering when the API or the database is down (a suspended Neon compute must not blank the demo).


@dataclass(frozen=True)
class Clock:
    """When a group's cached answers expire: minutes past the hour at the listed UTC hours."""

    name: str
    hours: tuple[int, ...]
    minute: int
    max_s: int  # never longer than this (also the browser cap's upper bound)
    swr_s: int  # stale-while-revalidate

    def seconds_to_next(self, now: datetime) -> int:
        """Seconds from `now` to the next expiry point (at least 60)."""
        from datetime import UTC, timedelta

        now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in (0, 1):
            for hour in sorted(self.hours):
                point = day + timedelta(days=offset, hours=hour, minutes=self.minute)
                if point > now:
                    return int(max(60, min(self.max_s, (point - now).total_seconds())))
        return self.max_s


_NIGHT = (0, 1, 3, 4, 6, 7, 21, 22)  # the 3-hourly night runs and their late-start catch points
HOURLY = Clock("hourly", tuple(sorted({*range(8, 20), *_NIGHT})), 30, 3 * 3600, 86400)
SIX_HOURLY = Clock("six_hourly", (0, 4, 6, 10, 11, 16, 17, 22), 30, 6 * 3600, 86400)
DAILY = Clock("daily", (13, 14), 30, 86400, 172800)
BULLETIN = Clock("bulletin", (2, 3, 4, 6, 7), 30, 86400, 86400)
WEEKLY = Clock("weekly", (3,), 30, 86400, 604800)
IMMUTABLE = "public, max-age=31536000, s-maxage=31536000, immutable"
STALE_IF_ERROR_S = 604800  # a week: the last good answer beats an error page during an outage
BROWSER_MAX_S = 300  # browsers come back to the CDN (cheap) at least this often


@dataclass(frozen=True)
class CDNRule:
    pattern: re.Pattern[str]
    clock: Clock | None  # None: never cached by the CDN
    immutable: bool = False
    finished_runs_only: bool = False  # agent runs: cache only once the run has finished
    private: bool = False  # browsers may keep it (the route's max-age); shared caches never


def _rule(pattern: str, clock: Clock | None, **flags: bool) -> CDNRule:
    return CDNRule(re.compile(pattern), clock, **flags)


# First match wins. Paths that match nothing (the docs, /openapi.json) get no CDN header.
CDN_RULES: tuple[CDNRule, ...] = (
    _rule(r"^/api/health$", None),  # a liveness probe must reach the function
    _rule(r"^/api/agent/ask\b", None),  # the SSE stream
    # The Gemini quota is the same for every visitor, so it shares the hourly clock. Uncached, the
    # minute poll of every open /live tab reached the database and kept the Neon compute awake for
    # as long as the tab stayed open. A copy up to an hour old is enough for the budget hint;
    # /api/agent/ask checks the real budget before every live question.
    _rule(r"^/api/agent/status\b", HOURLY),
    _rule(r"^/api/agent/runs/", DAILY, finished_runs_only=True),  # a finished run never changes
    _rule(r"^/api/imagery/(render|tiles/)", None, immutable=True),
    _rule(r"^/api/imagery/tilejson/", WEEKLY),
    _rule(r"^/api/imagery/(featured|scenes)\b", DAILY),
    _rule(r"^/api/geo/", WEEKLY),
    _rule(r"^/api/orbits/(tle|clock)\b", DAILY),
    _rule(r"^/api/orbits/validation\b", SIX_HOURLY),
    _rule(r"^/api/air/stubble-share\b", SIX_HOURLY),
    _rule(r"^/api/smoke/validation\b", DAILY),
    _rule(r"^/api/(smoke|met)/", SIX_HOURLY),
    _rule(r"^/api/scars/summary\b", HOURLY),  # carries FIRMS counts per district
    _rule(r"^/api/(scars|risk)\b", DAILY),
    _rule(r"^/api/bulletin/", BULLETIN),
    _rule(r"^/api/", HOURLY),  # fires, stats, sources, air, GK-2A, orbit passes, agent examples
)


def cdn_rule(path: str) -> CDNRule | None:
    return next((rule for rule in CDN_RULES if rule.pattern.match(path)), None)


_MAX_AGE = re.compile(r"(?:^|,)\s*max-age=(\d+)")


def cdn_cache_control(rule: CDNRule, current: str | None, now: datetime | None = None) -> str | None:
    """The Cache-Control for a 200 answer under `rule` at `now`, or None to leave the response as
    it is.

    The CDN keeps it until the rule's clock next ticks; the browser keeps the route's own max-age
    (the contract's numbers), capped by that and BROWSER_MAX_S. A route that set
    `no-store`/`no-cache`/`private` is never made public, and one that set its own `s-maxage` (a
    headline with a failed block: a short shared life) keeps it."""
    if current and "s-maxage" in current and not rule.immutable and not rule.private:
        return None
    if rule.immutable:
        return IMMUTABLE
    if rule.private:
        if not current or "no-store" in current or "private" in current:
            return None
        return re.sub(r"\bpublic\b", "private", current) if "public" in current else f"private, {current}"
    if rule.clock is None:
        return None
    if current and re.search(r"no-store|no-cache|private", current):
        return None
    from blindspot.provenance import utcnow

    ttl = rule.clock.seconds_to_next(now or utcnow())
    found = _MAX_AGE.search(current or "")
    short = min(int(found.group(1)), ttl, BROWSER_MAX_S) if found else min(ttl, 60)
    return (f"public, max-age={short}, s-maxage={ttl}, stale-while-revalidate={rule.clock.swr_s}, "
            f"stale-if-error={STALE_IF_ERROR_S}")


class CDNCacheHeaders:
    """ASGI middleware: sets the CDN Cache-Control (see CDN_RULES) on successful GET answers.

    Errors, other methods, the agent's SSE stream and agent runs still in progress keep the
    headers their route set. `Vary: Origin` (the CORS headers
    differ per origin, so a CDN must not hand one site's answer to another) comes from
    CORSMiddleware, which wraps this middleware and adds it to every response (with or without an
    Origin); adding it here as well gave `Vary: Origin, Accept-Encoding, Origin`.
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
