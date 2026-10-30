"""The one HTTP client that every upstream call goes through.

fetch() gives every module the same behaviour instead of seven slightly different ones:
- timeouts (connect 10 s, read 30 s) and a descriptive User-Agent;
- up to 3 attempts on timeouts, connection errors, 5xx and 429 (Retry-After honoured up to 60 s),
  with exponential backoff in between;
- one source_fetch row per call (ok or error, HTTP status, latency) with secrets masked in the URL;
- a per-source cooldown: after 3 failed calls in a row the source is paused for 5 minutes and
  fetch() raises SourceUnavailable at once, instead of hammering a service that is down.

Call it from plain `def` jobs and routes (FastAPI runs those in a thread pool).
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote_plus

import httpx
import orjson

from blindspot import __version__
from blindspot.config import get_settings
from blindspot.provenance import SourceRef, label_for, record_fetch, set_fetch_records, utcnow

__all__ = [
    "FetchError",
    "FetchResult",
    "SourceUnavailable",
    "close_client",
    "fetch",
    "get_client",
    "redact_url",
    "reset_cooldowns",
    "set_fetch_records",
]

log = logging.getLogger(__name__)

USER_AGENT = (
    f"BlindSpot/{__version__} (crop-residue fire monitoring for Punjab and Haryana; "
    f"python-httpx/{httpx.__version__})"
)
TIMEOUT = httpx.Timeout(30.0, connect=10.0)  # read, write and pool 30 s; connect 10 s
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0  # waits about 1 s, then 2 s, between attempts
MAX_RETRY_AFTER_S = 60.0  # the longest Retry-After we wait inside one call
COOLDOWN_AFTER_FAILURES = 3  # failed calls in a row before a source is paused
COOLDOWN_S = 300.0
# Query parameters masked in logged URLs (any case). Header values are never logged at all.
DEFAULT_REDACT = (
    "api-key", "api_key", "apikey", "key", "MAP_KEY", "X-API-Key", "token", "access_token", "sig",
)

Expect = Literal["json", "text", "bytes"]

# Worth retrying: the request may well work a moment later.
_TRANSIENT = (
    httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError,
)


class FetchError(RuntimeError):
    """An upstream call failed after its retries. The failure is already in source_fetch."""

    def __init__(
        self,
        source_key: str,
        message: str,
        *,
        status_code: int | None = None,
        fetch_id: int | None = None,
        url_logged: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{source_key}: {message}")
        self.source_key = source_key
        self.status_code = status_code
        self.fetch_id = fetch_id
        self.url_logged = url_logged
        self.retry_after_s = retry_after_s


class SourceUnavailable(FetchError):
    """The source is paused after repeated failures, so no request was sent."""


@dataclass(frozen=True)
class FetchResult:
    data: Any  # parsed JSON, str or bytes, according to `expect`
    fetch_id: int | None  # the source_fetch row; None only if the database was unreachable
    status_code: int
    latency_ms: int  # of the attempt that succeeded
    url_logged: str  # the URL as recorded, with secrets replaced by ***
    source_key: str = ""
    fetched_at: datetime | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def ref(self) -> SourceRef:
        """This fetch as an entry for a response's `sources` list."""
        return SourceRef(
            key=self.source_key,
            label=label_for(self.source_key),
            url=self.url_logged,
            fetched_at=self.fetched_at,
            fetch_id=self.fetch_id,
        )


def redact_url(url: str, names: Iterable[str] = DEFAULT_REDACT) -> str:
    """Mask the query parameters named in `names`, then any configured API key anywhere in the
    URL, which also catches keys in the path (FIRMS puts its MAP_KEY there)."""
    hidden = {name.lower() for name in names}
    base, sep, query = url.partition("?")
    if sep:
        pieces = []
        for piece in query.split("&"):
            name, eq, _value = piece.partition("=")
            pieces.append(f"{name}=***" if eq and unquote_plus(name).lower() in hidden else piece)
        url = f"{base}?{'&'.join(pieces)}"
    return get_settings().redact(url)


# --- the shared client --------------------------------------------------------------------------

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def get_client() -> httpx.Client:
    """The process-wide client, so every module shares one connection pool. Thread-safe."""
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


# --- per-source cooldown (in memory, per process) -----------------------------------------------

_state_lock = threading.Lock()
_failures: dict[str, int] = {}  # failed calls in a row, per source
_paused_until: dict[str, float] = {}  # time.monotonic() deadline, per source
_last_error: dict[str, str] = {}


def reset_cooldowns() -> None:
    """Forget every failure streak and pause (used by tests)."""
    with _state_lock:
        _failures.clear()
        _paused_until.clear()
        _last_error.clear()


def _raise_if_paused(source_key: str) -> None:
    with _state_lock:
        remaining = _paused_until.get(source_key, 0.0) - time.monotonic()
        last_error = _last_error.get(source_key, "unknown")
    if remaining > 0:
        raise SourceUnavailable(
            source_key,
            f"{label_for(source_key)} is paused for another {remaining:.0f} s after repeated "
            f"failures (last error: {last_error})",
            retry_after_s=remaining,
        )


def _note_success(source_key: str) -> None:
    with _state_lock:
        _failures.pop(source_key, None)
        _paused_until.pop(source_key, None)
        _last_error.pop(source_key, None)


def _note_failure(
    source_key: str, error: str, *, outage: bool, retry_after_s: float | None
) -> None:
    with _state_lock:
        if not outage:  # the server answered (say a 404), so it is up: the streak ends
            _failures.pop(source_key, None)
            return
        count = _failures.get(source_key, 0) + 1
        _failures[source_key] = count
        _last_error[source_key] = error
        pause = 0.0
        if retry_after_s is not None and retry_after_s > MAX_RETRY_AFTER_S:
            pause = retry_after_s  # the server told us how long to stay away
        elif count >= COOLDOWN_AFTER_FAILURES:
            pause = COOLDOWN_S
        if pause:
            deadline = time.monotonic() + pause
            _paused_until[source_key] = max(_paused_until.get(source_key, 0.0), deadline)
    if pause:
        log.warning("%s paused for %.0f s (%d failed calls in a row)", source_key, pause, count)


# --- one attempt ---------------------------------------------------------------------------------


@dataclass
class _Attempt:
    ok: bool = False
    payload: Any = None
    status: int | None = None
    headers: httpx.Headers | None = None
    size: int = 0
    error: str = ""
    transient: bool = False  # worth retrying
    outage: bool = False  # counts towards the source's cooldown
    retry_after_s: float | None = None
    latency_ms: int = 0


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    try:
        return " ".join(response.text.split())[:limit]
    except Exception:
        return ""


def _retry_after_s(response: httpx.Response) -> float | None:
    """Retry-After as seconds; the header may be a number of seconds or an HTTP date."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - utcnow()).total_seconds())


def _parse(response: httpx.Response, expect: Expect) -> Any:
    if expect == "bytes":
        return response.content
    if expect == "text":
        return response.text
    return orjson.loads(response.content) if response.content else None


def _attempt(client: httpx.Client, request: httpx.Request, expect: Expect) -> _Attempt:
    t0 = time.perf_counter()
    try:
        response = client.send(request)
    except _TRANSIENT as exc:
        return _Attempt(error=_describe(exc), transient=True, outage=True, latency_ms=_ms(t0))
    except httpx.HTTPError as exc:  # a bad URL or protocol: retrying will not help
        return _Attempt(error=_describe(exc), latency_ms=_ms(t0))
    status = response.status_code
    a = _Attempt(
        status=status, headers=response.headers, size=len(response.content), latency_ms=_ms(t0)
    )
    if response.is_success or status == 304:
        try:
            a.payload, a.ok = _parse(response, expect), True
        except ValueError as exc:  # orjson.JSONDecodeError is a ValueError
            a.error, a.outage = f"response is not valid {expect}: {exc}", True
        return a
    a.error = f"HTTP {status}: {_snippet(response)}"
    a.transient = a.outage = status >= 500 or status == 429
    if status in (429, 503):
        a.retry_after_s = _retry_after_s(response)
    return a


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


# --- fetch ---------------------------------------------------------------------------------------


def fetch(
    source_key: str,
    url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    data: Mapping[str, Any] | None = None,
    content: bytes | str | None = None,
    expect: Expect = "json",
    redact: Iterable[str] = DEFAULT_REDACT,
    timeout: float | httpx.Timeout | None = None,
    meta: dict[str, Any] | None = None,
) -> FetchResult:
    """Call an upstream API and record the call in source_fetch.

    `data` sends form fields and `content` a raw body; `meta` is stored on the source_fetch row.
    Returns a FetchResult whose `data` is parsed JSON, text or bytes (per `expect`). Raises
    SourceUnavailable while the source is paused, and FetchError when the call still fails after
    its retries; a 4xx other than 429 fails at once, since retrying would not fix it. Call
    set_fetch_records(result.fetch_id, n) once you know how many records the response held.
    """
    if expect not in ("json", "text", "bytes"):
        raise ValueError(f"expect must be 'json', 'text' or 'bytes', not {expect!r}")
    _raise_if_paused(source_key)
    client = get_client()
    method = method.upper()

    def build() -> httpx.Request:
        return client.build_request(
            method,
            url,
            params=params,
            headers=headers,
            json=json,
            data=data,
            content=content,
            timeout=timeout or TIMEOUT,
        )

    settings = get_settings()
    url_logged = redact_url(str(build().url), redact)
    started_at = utcnow()
    attempt = 0
    while True:
        attempt += 1
        result = _attempt(client, build(), expect)
        if result.ok or not result.transient or attempt >= MAX_ATTEMPTS:
            break
        if result.retry_after_s is not None and result.retry_after_s > MAX_RETRY_AFTER_S:
            break  # too long to wait inside a call; the source is paused instead (below)
        if result.retry_after_s is not None:
            wait = result.retry_after_s
        else:
            wait = BACKOFF_BASE_S * 2 ** (attempt - 1) * random.uniform(1.0, 1.25)
        log.warning(
            "%s attempt %d/%d failed (%s); retrying in %.1f s",
            source_key, attempt, MAX_ATTEMPTS, settings.redact(result.error), wait,
        )
        time.sleep(wait)

    finished_at = utcnow()
    call_meta = {"method": method, "attempts": attempt, **(meta or {})}
    if result.ok:
        call_meta["bytes"] = result.size
        fetch_id = record_fetch(
            source_key,
            url_logged,
            ok=True,
            started_at=started_at,
            finished_at=finished_at,
            http_status=result.status,
            latency_ms=result.latency_ms,
            meta=call_meta,
        )
        _note_success(source_key)
        log.info(
            "%s %s %s -> %s in %d ms",
            source_key, method, url_logged, result.status, result.latency_ms,
        )
        return FetchResult(
            data=result.payload,
            fetch_id=fetch_id,
            status_code=result.status or 0,
            latency_ms=result.latency_ms,
            url_logged=url_logged,
            source_key=source_key,
            fetched_at=finished_at,
            headers=result.headers or {},
        )

    error = settings.redact(result.error)
    fetch_id = record_fetch(
        source_key,
        url_logged,
        ok=False,
        started_at=started_at,
        finished_at=finished_at,
        http_status=result.status,
        latency_ms=result.latency_ms,
        error=error,
        meta=call_meta,
    )
    _note_failure(source_key, error, outage=result.outage, retry_after_s=result.retry_after_s)
    log.warning(
        "%s %s %s failed after %d attempt(s): %s", source_key, method, url_logged, attempt, error
    )
    raise FetchError(
        source_key,
        error,
        status_code=result.status,
        fetch_id=fetch_id,
        url_logged=url_logged,
        retry_after_s=result.retry_after_s,
    )
