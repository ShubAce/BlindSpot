"""The investigator: Gemini's manual function-calling loop, streamed as SSE events.

One question (POST /api/agent/ask) goes like this:

1. `run{run_id, model}`.
2. Up to MAX_ROUNDS tool rounds. In each, Gemini streams its thought summary (`thought`), then
   either asks for tools or writes the answer. We run the requested tools in parallel, in-process
   against our own API (`tool_call`, then `map` for show_on_map, then `tool_result`), and send every
   result back in one user turn, each echoing its call id. Text is streamed as `answer_delta`.
3. The verifier checks every number in the answer against what the tools returned. If any number
   is not found, the model gets ONE revision pass with the list. Then `answer{text, citations}`
   and `verification{checked, verified, unverified, passed}`.
4. `done{run_id, duration_ms}`, always last; failures add `error{message, retryable}` before it.

A follow-up question (`follow_up_of`, the id of an earlier investigation) also gets the last
earlier questions and answers of its thread, at most MAX_CONTEXT_PAIRS of them, as a context part
in front of the question: answers only, without their citation markers or any tool output. They
help the model understand "and there?", but they are never evidence: the verifier still checks
the new answer against this run's own tool results, so every figure is fetched again. The `run`
event (stored with the run) records which run was followed up and the exact context given.

Why a manual loop (not the SDK's automatic function calling): we need each step as an event, the
citations, a model fallback in the middle of a run, and a hard cap on rounds.

Robustness, because judges watch live and the free tier is small:
- Gateway: a model fallback chain. A 429 or 503 cools that model down (honouring Google's
  RetryInfo, an hour for a daily quota) and the call moves to the next model at once, carrying the
  history over (thought signatures swapped for Google's documented skip value).
- A client-side requests-per-minute guard and a daily budget per model, counted from source_fetch
  (every Gemini call is recorded there like any other upstream call), that keeps a 20% reserve:
  new questions stop at 80% of a model's daily quota; the bulletin and runs already under way may
  use the reserve.
- When no model can answer, the run still shows the evidence: it runs the core tools without the
  model and says "AI narrative unavailable". It never shows a canned answer.
- The same normalised question within 10 minutes replays the stored run instead of calling
  Gemini again, and identical questions asked at the same moment share one run (single flight).
- A per-client rate limit protects the quota from one noisy client: in memory, and on serverless
  deployments also counted in Postgres (blindspot.models.client_quota) so that it holds across
  instances and cold starts. Joined and replayed runs are never counted.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import errors, types
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from blindspot.agent import prompts
from blindspot.agent.tools import (
    TOOLS,
    CitationBook,
    InternalApi,
    ToolResult,
    declarations,
    model_payload,
    run_tool,
    unseen_audit_block,
)
from blindspot.agent.verifier import (
    Attributed,
    Evidence,
    HonestyFacts,
    build_evidence,
    check_honesty,
    cited_markers,
    honest_fallback,
    honesty_facts,
    verify,
)
from blindspot.config import ENV_FILE, get_settings
from blindspot.provenance import record_fetch, utcnow

log = logging.getLogger(__name__)

MAX_ROUNDS = 8  # tool rounds per question (architecture.md section 7)
MAX_CALLS_PER_ROUND = 8  # parallel calls answered in one round; extra calls get an error
HELD_TEXT_CHARS = 160  # text held back in a turn until we know it is not a preamble to a tool call
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
# Google resets the free daily quota at midnight Pacific time (ai.google.dev rate-limits page,
# docs/research/gemini-agent.md section 1).
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
# The documented value that tells Gemini to skip thought-signature validation, used when a history
# written by one model continues on another (docs/research/gemini-agent.md sections 3-4).
SKIP_SIGNATURE = base64.urlsafe_b64decode("skip_thought_signature_validator")

# Free-tier quotas per model. Google no longer publishes them; these are the community-measured
# values recorded in docs/research/gemini-agent.md section 1 (Flash about 5 RPM / 20 RPD,
# Flash-Lite about 10 RPM / 500 RPD). Override with AGENT_DAILY_LIMITS / AGENT_MINUTE_LIMITS
# ("model=n,model=n") once the real numbers are read from aistudio.google.com/rate-limit.
DEFAULT_RPD = {
    "gemini-3.5-flash-lite": 500,
    "gemini-3.1-flash-lite": 500,
    "gemini-3.8-flash": 20,
    "gemini-3.6-flash": 20,
    "gemini-3-flash-preview": 20,
}
DEFAULT_RPM = {
    "gemini-3.5-flash-lite": 10,
    "gemini-3.1-flash-lite": 10,
    "gemini-3.8-flash": 5,
    "gemini-3.6-flash": 5,
    "gemini-3-flash-preview": 5,
}
FALLBACK_RPD, FALLBACK_RPM = 20, 5  # for a model not listed above: assume the smallest bucket


class AgentSettings(BaseSettings):
    """Agent knobs, read from AGENT_* environment variables or the repo-root .env."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="AGENT_", env_ignore_empty=True, extra="ignore"
    )

    chain: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash,gemini-3.6-flash"
    bulletin_chain: str = "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite"
    daily_limits: str = ""  # "gemini-3.8-flash=20,gemini-3.5-flash-lite=500"
    minute_limits: str = ""
    reserve: float = 0.2  # share of each model's daily quota kept back from new questions
    ip_per_minute: int = 3
    ip_per_day: int = 40
    cache_minutes: float = 10.0
    max_wait_s: float = 12.0  # how long a call may wait for a model to come off cooldown
    timeout_ms: int = 90_000
    # Our own deadline for silence on a stream (first chunk or between chunks). Gemini has held a
    # stream open for 90-145 s before answering 503/504 (source_fetch, 2026-10-31); nobody should
    # watch a frozen answer that long when another model can take the call.
    idle_s: float = 35.0
    bulletin_idle_s: float = 60.0  # the morning job has no one waiting on it
    # How long after it started an investigation can be followed up. The air, fire and wind data
    # an answer describes change by the hour; a day-old answer is no context for "and now?".
    follow_up_hours: float = 24.0


def agent_settings() -> AgentSettings:
    return AgentSettings()


def _parse_limits(raw: str) -> dict[str, int]:
    out = {}
    for item in raw.split(","):
        name, _, value = item.partition("=")
        if name.strip() and value.strip().isdigit():
            out[name.strip()] = int(value)
    return out


def chain_for(kind: str) -> list[str]:
    s = agent_settings()
    raw = s.bulletin_chain if kind == "bulletin" else s.chain
    return [m.strip() for m in raw.split(",") if m.strip()]


def daily_limit(model: str) -> int:
    return {**DEFAULT_RPD, **_parse_limits(agent_settings().daily_limits)}.get(model, FALLBACK_RPD)


def minute_limit(model: str) -> int:
    return {**DEFAULT_RPM, **_parse_limits(agent_settings().minute_limits)}.get(model, FALLBACK_RPM)


# --- the daily budget (counted from source_fetch, shared by every process) --------------------


def quota_day_start(now: datetime | None = None) -> datetime:
    """When Google's current free-tier day began: the last midnight in Pacific time, as UTC."""
    local = (now or utcnow()).astimezone(QUOTA_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


_USED_SQL = text(
    """
    SELECT meta->>'model' AS model, count(*) AS n
    FROM source_fetch
    WHERE source_key = 'gemini' AND started_at >= :since
    GROUP BY 1
    """
)


def used_today() -> dict[str, int]:
    """Gemini requests per model since the quota day began (every attempt counts, conservatively)."""
    from blindspot.db import SessionLocal

    try:
        with SessionLocal() as session:
            rows = session.execute(_USED_SQL, {"since": quota_day_start()}).all()
        return {str(model): int(n) for model, n in rows if model}
    except Exception as exc:  # no database: the in-memory cooldowns still protect the quota
        log.warning("could not count today's Gemini calls: %s", exc)
        return {}


# --- per-model cooldowns and the per-minute guard (in memory, per process) ----------------------

_lock = threading.Lock()
_cool_until: dict[str, float] = {}
_cool_reason: dict[str, str] = {}
_sent: dict[str, deque[float]] = defaultdict(deque)


def reset_gateway_state() -> None:
    """Forget cooldowns and the per-minute window (tests)."""
    with _lock:
        _cool_until.clear()
        _cool_reason.clear()
        _sent.clear()


def _cool(model: str, seconds: float, reason: str) -> None:
    with _lock:
        _cool_until[model] = max(_cool_until.get(model, 0.0), time.time() + seconds)
        _cool_reason[model] = reason
    log.warning("gemini model %s cooling down for %.0f s: %s", model, seconds, reason)


def cool_after(model: str, exc: BaseException) -> None:
    """Rest `model` as long as this error calls for (a stream that broke mid-way, say)."""
    _cool(model, *_cooldown_for(exc))


def _ready_at(model: str) -> float:
    """Unix time at which `model` may be called again (cooldown and requests-per-minute)."""
    with _lock:
        window = _sent[model]
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()
        rpm_at = window[0] + 60 if len(window) >= minute_limit(model) else 0.0
        return max(_cool_until.get(model, 0.0), rpm_at)


def _note_sent(model: str) -> None:
    with _lock:
        _sent[model].append(time.time())


def model_status(kind: str = "ask") -> list[dict[str, Any]]:
    """Per model: today's use, its limit, the reserve line and any cooldown (GET /api/agent/status)."""
    used = used_today()
    reserve = agent_settings().reserve
    out = []
    for model in dict.fromkeys(chain_for("ask") + chain_for("bulletin")):
        limit = daily_limit(model)
        ready = _ready_at(model)
        out.append(
            {
                "model": model,
                "used_today": used.get(model, 0),
                "daily_limit": limit,
                "new_questions_stop_at": int(limit * (1 - reserve)),
                "cooling_down_until": datetime.fromtimestamp(ready, UTC)
                if ready > time.time()
                else None,
                "cooldown_reason": _cool_reason.get(model) if ready > time.time() else None,
                "in_chain": {"ask": model in chain_for("ask"),
                             "bulletin": model in chain_for("bulletin")},  # fmt: skip
            }
        )
    return out


# --- the gateway: one streamed Gemini call with fallback ----------------------------------------


class GeminiUnavailable(RuntimeError):
    """No model in the chain could take the call (quota, outage, missing key)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class StreamInterrupted(GeminiUnavailable):
    """A stream broke after it started (Gemini sends 503 "high demand" mid-stream too). When
    nothing of the answer was shown yet, the turn can be retried on another model."""

    def __init__(self, message: str, *, answer_shown: bool) -> None:
        super().__init__(message, retryable=True)
        self.answer_shown = answer_shown


def _error_payload(exc: errors.APIError) -> dict[str, Any]:
    details = exc.details if isinstance(exc.details, dict) else {}
    return details.get("error", details) if isinstance(details, dict) else {}


def _cooldown_for(exc: BaseException) -> tuple[float, str]:
    """How long to rest a model after this error, and why (docs/research/gemini-agent.md s5)."""
    if isinstance(exc, errors.APIError):
        payload = _error_payload(exc)
        message = str(payload.get("message") or exc.message or "")
        code = exc.code or 0
        if code == 429:
            items = payload.get("details") or []
            quota_ids = " ".join(
                str(v.get("quotaId", ""))
                for d in items if isinstance(d, dict)
                for v in d.get("violations", []) or []
            )  # fmt: skip
            if "PerDay" in quota_ids or "limit: 0" in message:
                return 3600.0, "daily quota exhausted (resets midnight Pacific)"
            for d in items:
                if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo"):
                    delay = str(d.get("retryDelay", "30s")).rstrip("s")
                    try:
                        return max(float(delay), 1.0), "rate limited (RetryInfo)"
                    except ValueError:
                        break
            found = re.search(r"retry in ([\d.]+)s", message)
            return (float(found.group(1)) if found else 30.0), "rate limited"
        if code == 503:
            return 30.0, "model overloaded (503)"
        if code == 404:
            return 6 * 3600.0, "model not available to this key (404)"
        if code == 400:
            return 600.0, f"request rejected (400): {message[:120]}"
        if code >= 500 or code == 408:
            return 10.0, f"server error ({code})"
        return 60.0, f"HTTP {code}"
    return 5.0, f"network: {type(exc).__name__}"


def _fatal(exc: BaseException) -> bool:
    """Errors no other model can fix: the key or the project is refused."""
    return isinstance(exc, errors.APIError) and exc.code in (401, 403)


def _neutralise(contents: list[types.Content]) -> list[types.Content]:
    """A history written by one model, made acceptable to another: thought signatures on function
    calls are swapped for the documented skip value (signatures are only valid for their model)."""
    out = [c.model_copy(deep=True) for c in contents]
    for content in out:
        if content.role != "model":
            continue
        for part in content.parts or []:
            if part.function_call is not None and part.thought_signature:
                part.thought_signature = SKIP_SIGNATURE
    return out


ClientFactory = Callable[[], Any]


def default_client_factory() -> Any:
    """A google-genai client. Retries only 408/5xx itself: 429/503 go to the fallback chain."""
    key = get_settings().gemini_api_key
    if key is None:
        raise GeminiUnavailable("GEMINI_API_KEY is not set", retryable=False)
    return genai.Client(
        api_key=key.get_secret_value(),
        http_options=types.HttpOptions(
            timeout=agent_settings().timeout_ms,
            retry_options=types.HttpRetryOptions(
                attempts=2, initial_delay=1, max_delay=4, http_status_codes=[408, 500, 502, 504]
            ),
        ),
    )


@dataclass
class CallLog:
    """One Gemini request, for agent_run.usage."""

    model: str
    ok: bool
    latency_ms: int
    fetch_id: int | None = None
    finish: str | None = None
    tokens: dict[str, int | None] = field(default_factory=dict)
    error: str | None = None


class Gateway:
    """Chooses a model for each call and streams it, falling back along the chain."""

    def __init__(self, kind: str, run_id: str, client_factory: ClientFactory | None = None) -> None:
        self.kind = kind
        self.run_id = run_id
        self.chain = chain_for(kind)
        self._factory = client_factory or default_client_factory
        self._client: Any = None
        self.calls: list[CallLog] = []

    def client(self) -> Any:
        if self._client is None:
            self._client = self._factory()
        return self._client

    async def aclose(self) -> None:
        if self._client is None:
            return
        aio = getattr(self._client, "aio", None)
        try:
            if aio is not None and hasattr(aio, "aclose"):
                await aio.aclose()
            if hasattr(self._client, "close"):
                self._client.close()
        except Exception:  # closing must never fail a run
            log.debug("closing the Gemini client failed", exc_info=True)

    def _over_budget(self, model: str, used: dict[str, int], priority: bool) -> bool:
        limit = daily_limit(model)
        share = 1.0 if (priority or self.kind == "bulletin") else 1.0 - agent_settings().reserve
        return used.get(model, 0) >= int(limit * share)

    async def first_choice(self) -> str | None:
        """The model a new run would start on now, or None when none is available."""
        used = await asyncio.to_thread(used_today)
        now = time.time()
        for model in self.chain:
            if _ready_at(model) <= now and not self._over_budget(model, used, priority=False):
                return model
        return None

    async def stream(
        self,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        *,
        prefer: str | None,
        priority: bool,
    ) -> tuple[str, AsyncIterator[types.GenerateContentResponse], list[types.Content]]:
        """Start a streamed call on the best available model.

        Returns (model, chunks, contents actually sent). HTTP errors surface on the first chunk,
        so the first chunk is read here, inside the fallback loop. If the model differs from
        `prefer`, the history was neutralised and the caller must continue with the returned
        contents. Raises GeminiUnavailable when no model can take the call.
        """
        last_error = "no model in the chain"
        settings = agent_settings()
        rejected = 0  # 400s in this call: usually the request itself, so no model is cooled
        for attempt in range(2):  # second pass: wait for the soonest cooldown if it is short
            used = await asyncio.to_thread(used_today)
            order = sorted(self.chain, key=lambda m: m != prefer)  # stay on the history's model
            for model in order:
                if _ready_at(model) > time.time():
                    last_error = f"{model}: {_cool_reason.get(model) or 'per-minute limit'}"
                    continue
                if self._over_budget(model, used, priority):
                    last_error = f"{model}: daily budget used (reserve kept)"
                    continue
                payload = contents if prefer in (None, model) else _neutralise(contents)
                try:
                    chunks = await self._open(model, payload, config)
                except GeminiUnavailable:
                    raise
                except Exception as exc:
                    if _fatal(exc):
                        raise GeminiUnavailable(
                            "Gemini refused the API key or project", retryable=False
                        ) from exc
                    if isinstance(exc, errors.APIError) and exc.code == 400:
                        rejected += 1
                        last_error = f"{model}: request rejected (400): {str(exc.message)[:160]}"
                        log.warning("gemini %s", last_error)
                        if rejected >= 2:  # two models refused the same request: stop here
                            raise GeminiUnavailable(
                                f"Gemini rejected the request ({last_error})", retryable=False
                            ) from exc
                        continue  # some 400s are model-specific (a mode it lacks): try another
                    seconds, reason = _cooldown_for(exc)
                    _cool(model, seconds, reason)
                    last_error = f"{model}: {reason}"
                    continue
                return model, chunks, payload
            if attempt == 0:
                wait = min(_ready_at(m) for m in self.chain) - time.time()
                if 0 < wait <= settings.max_wait_s:
                    await asyncio.sleep(wait + 0.2)
                    continue
            break
        raise GeminiUnavailable(f"no Gemini model is available right now ({last_error})")

    async def _open(
        self, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Send the request, read the first chunk, and return an iterator over all chunks that
        records the call in source_fetch (and agent_run.usage) when the stream ends."""
        client = self.client()  # raises GeminiUnavailable without a key: nothing was sent
        _note_sent(model)
        started_at = utcnow()
        t0 = time.perf_counter()
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=contents, config=config
            )
            iterator = stream.__aiter__()
            try:
                first = await self._next(iterator)
            except StopAsyncIteration:
                first = None
        except BaseException as exc:
            await self._record(model, started_at, t0, None, exc)
            raise
        return self._chunks(model, started_at, t0, first, iterator)

    async def _next(
        self, iterator: AsyncIterator[types.GenerateContentResponse]
    ) -> types.GenerateContentResponse:
        """The next chunk, or a 504 ServerError after `idle_s` of silence, so the usual
        fallback (cool this model, try the next) applies to a stalled stream too."""
        settings = agent_settings()
        idle = settings.bulletin_idle_s if self.kind == "bulletin" else settings.idle_s
        try:
            return await asyncio.wait_for(iterator.__anext__(), idle)
        except TimeoutError:
            raise errors.ServerError(504, {"error": {
                "code": 504, "status": "DEADLINE_EXCEEDED",
                "message": f"no data from Gemini for {idle:.0f} s (BlindSpot's stream deadline)",
            }}) from None  # fmt: skip

    async def _chunks(
        self,
        model: str,
        started_at: datetime,
        t0: float,
        first: types.GenerateContentResponse | None,
        iterator: AsyncIterator[types.GenerateContentResponse],
    ) -> AsyncIterator[types.GenerateContentResponse]:
        last = first
        try:
            if first is not None:
                yield first
                while True:
                    try:
                        chunk = await self._next(iterator)
                    except StopAsyncIteration:
                        break
                    last = chunk
                    yield chunk
        except BaseException as exc:
            await self._record(model, started_at, t0, last, exc)
            raise
        await self._record(model, started_at, t0, last, None)

    async def _record(
        self,
        model: str,
        started_at: datetime,
        t0: float,
        last: types.GenerateContentResponse | None,
        exc: BaseException | None,
    ) -> None:
        latency = round((time.perf_counter() - t0) * 1000)
        usage = getattr(last, "usage_metadata", None)
        tokens = {
            "prompt": getattr(usage, "prompt_token_count", None),
            "output": getattr(usage, "candidates_token_count", None),
            "thoughts": getattr(usage, "thoughts_token_count", None),
            "cached": getattr(usage, "cached_content_token_count", None),
        }
        finish = None
        if last is not None and last.candidates and last.candidates[0].finish_reason:
            finish = last.candidates[0].finish_reason.name
        status = exc.code if isinstance(exc, errors.APIError) else (200 if exc is None else None)
        error = None
        if exc is not None:
            error = get_settings().redact(f"{type(exc).__name__}: {exc}")[:500]
        fetch_id = await asyncio.to_thread(
            record_fetch,
            "gemini",
            GEMINI_URL.format(model=model),
            ok=exc is None,
            started_at=started_at,
            http_status=status,
            records=1 if exc is None else 0,
            latency_ms=latency,
            error=error,
            meta={"model": model, "kind": self.kind, "run_id": self.run_id, "tokens": tokens,
                  "finish": finish},  # fmt: skip
        )
        self.calls.append(CallLog(model, exc is None, latency, fetch_id, finish, tokens, error))


# --- runs: events, single flight, replay ---------------------------------------------------------


def normalise_question(question: str) -> str:
    """Case, punctuation and spacing do not make a question different."""
    folded = unicodedata.normalize("NFKC", question).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", folded).split())


def cache_key(question: str, lang: str, follow_up_of: str | None = None) -> str:
    """sha256 of the language and the normalised question. A follow-up adds the run it follows:
    "and yesterday?" after one answer is not the same question as after another, nor on its own.
    Keys of questions asked on their own are unchanged, so stored answers still replay."""
    key = f"{lang}|{normalise_question(question)}"
    if follow_up_of:
        key += f"|follow-up-of:{follow_up_of}"
    return hashlib.sha256(key.encode()).hexdigest()


def detect_lang(question: str) -> str:
    """hi for Devanagari, pa for Gurmukhi, else en (when the client did not say)."""
    if re.search(r"[਀-੿]", question):
        return "pa"
    if re.search(r"[ऀ-ॿ]", question):
        return "hi"
    return "en"


@dataclass
class Run:
    """A run in progress: its events so far, which any number of listeners can follow."""

    id: str
    kind: str
    question: str
    lang: str
    cache_key: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    events: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    answer: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    model: str | None = None
    ok: bool = False
    error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int | None = None
    announced_model: str | None = None  # the model the last `run` event named
    follow_up_of: str | None = None  # the run this question follows up, if any
    # The earlier questions and answers the model is given as context, oldest first:
    # [{run_id, question, answer}] (see follow_up_context). Never evidence.
    context: list[dict[str, str]] = field(default_factory=list)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {"type": kind, **fields}
        if kind == "run":
            self.announced_model = fields.get("model")
        self.events.append(event)
        self._changed.set()
        return event

    def finish(self) -> None:
        self.finished = True
        self._changed.set()

    async def follow(self) -> AsyncIterator[dict[str, Any]]:
        """Every event from the first, then new ones as they come, until the run finishes."""
        index = 0
        while True:
            while index < len(self.events):
                yield self.events[index]
                index += 1
            if self.finished:
                return
            self._changed.clear()
            if index < len(self.events) or self.finished:
                continue
            await self._changed.wait()


_inflight: dict[str, Run] = {}
_tasks: set[asyncio.Task[None]] = set()


# --- one investigation ----------------------------------------------------------------------------


def _config(
    *, mode: str, thinking: str, system: str, with_tools: bool = True
) -> types.GenerateContentConfig:
    # No temperature/top_p/top_k: Gemini 3.6+ ignores them and future models reject them
    # (docs/research/gemini-agent.md decision 5).
    return types.GenerateContentConfig(
        system_instruction=system,
        tools=[types.Tool(function_declarations=declarations())] if with_tools else None,
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode=mode))
        if with_tools
        else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level=thinking, include_thoughts=True),
    )


@dataclass
class Turn:
    model: str
    content: types.Content | None
    calls: list[types.FunctionCall]
    text: str
    finish: str | None
    blocked: str | None = None


async def model_turn(
    gateway: Gateway,
    run: Run,
    contents: list[types.Content],
    config: types.GenerateContentConfig,
    *,
    prefer: str | None,
    stream_text: bool,
) -> tuple[Turn, list[types.Content]]:
    """One streamed model turn. Emits thoughts and (when `stream_text`) answer deltas. Keeps
    every part, because a thought signature can ride on an empty text part."""
    model, chunks, sent = await gateway.stream(
        contents, config, prefer=prefer, priority=bool(gateway.calls)
    )
    if model != run.announced_model:  # a fallback: tell the UI which model really answers
        run.emit("run", run_id=run.id, model=model, switched_from=run.announced_model)
    parts: list[types.Part] = []
    calls: list[types.FunctionCall] = []
    texts: list[str] = []
    held: list[str] = []
    finish: str | None = None
    blocked: str | None = None
    streaming = False  # the answer has started streaming as answer_delta
    try:
        async for chunk in chunks:
            if not chunk.candidates:
                feedback = getattr(chunk, "prompt_feedback", None)
                if feedback is not None and feedback.block_reason:
                    blocked = str(feedback.block_reason.name)
                continue
            candidate = chunk.candidates[0]
            if candidate.finish_reason:
                finish = candidate.finish_reason.name
            for part in (candidate.content.parts or []) if candidate.content else []:
                parts.append(part)
                if part.function_call is not None:
                    calls.append(part.function_call)
                elif part.text and part.thought:
                    run.emit("thought", text=part.text)
                elif part.text:
                    texts.append(part.text)
                    if not stream_text or calls:
                        continue
                    if streaming:
                        run.emit("answer_delta", text=part.text)
                        continue
                    held.append(part.text)
                    if sum(len(t) for t in held) >= HELD_TEXT_CHARS:
                        run.emit("answer_delta", text="".join(held))
                        held.clear()
                        streaming = True
    except (errors.APIError, httpx.HTTPError) as exc:  # the stream broke after it started
        cool_after(model, exc)
        raise StreamInterrupted(f"the Gemini stream from {model} was interrupted "
                                f"({type(exc).__name__})", answer_shown=streaming) from exc  # fmt: skip
    if held:  # the turn ended: short text before tool calls is narration, otherwise the answer
        if calls:
            run.emit("thought", text="".join(held))
        elif stream_text:
            run.emit("answer_delta", text="".join(held))
    content = types.Content(role="model", parts=parts) if parts else None
    return Turn(model, content, calls, "".join(texts).strip(), finish, blocked), sent


async def _run_calls(
    api: InternalApi, run: Run, book: CitationBook, calls: list[types.FunctionCall]
) -> list[types.Part]:
    """Run one round of tool calls in parallel; return the FunctionResponse parts, in call order."""
    ids = [call.id or f"call_{len(run.tools) + i + 1}" for i, call in enumerate(calls)]
    for call_id, call in zip(ids, calls, strict=True):
        run.emit("tool_call", id=call_id, name=call.name, args=dict(call.args or {}))

    async def one(index: int, call: types.FunctionCall) -> tuple[int, ToolResult]:
        if index >= MAX_CALLS_PER_ROUND:
            return index, ToolResult(False, error="too many tool calls in one turn; ask again")
        return index, await run_tool(api, call.name or "", dict(call.args or {}))

    results: dict[int, tuple[ToolResult, dict[str, Any]]] = {}
    for future in asyncio.as_completed([one(i, c) for i, c in enumerate(calls)]):
        index, result = await future
        payload = model_payload(result, book)  # first: the markers the model sees come first
        results[index] = (result, payload)
        call = calls[index]
        if result.map_event:
            run.emit("map", **result.map_event)
        run.emit(
            "tool_result",
            id=ids[index],
            name=call.name,
            ok=result.ok,
            summary=result.summary if result.ok else f"{result.summary} ({result.error})",
            sources=book.for_client(result.sources),
        )
    parts = []
    for index, call in enumerate(calls):
        result, payload = results[index]
        run.tools.append({"id": ids[index], "name": call.name, "args": dict(call.args or {}),
                          "ok": result.ok, **payload})  # fmt: skip
        parts.append(
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response=payload
                )
            )
        )
    return parts


# When the model did not call show_on_map, the map still shows the layer of the main evidence it
# used: (tool, layer), most specific first. A UI hint, flagged auto=True, never data.
_AUTO_LAYERS = (
    ("trace_air_back", "smoke_back"),
    ("get_burn_scars", "scars"),
    ("get_burn_risk", "risk"),
    ("forecast_smoke", "smoke_forward"),
    ("get_fire_activity", "fires"),
    ("get_air_quality", "air_stations"),
    ("get_weather", "wind"),
    ("get_satellite_passes", "satellite_passes"),
)


def _auto_map_event(run: Run) -> dict[str, Any] | None:
    if any(e["type"] == "map" for e in run.events):
        return None
    used = {t["name"]: t for t in run.tools if t.get("ok")}
    for tool, layer in _AUTO_LAYERS:
        if tool in used:
            payload: dict[str, Any] = {"layer": layer, "visible": True, "auto": True}
            city = (used[tool].get("args") or {}).get("city")
            if layer == "smoke_back" and city:
                payload["params"] = {"city": city}
            return {"action": "show_layer", "payload": payload}
    return None


def attributed(tools_seen: list[dict[str, Any]]) -> list[Attributed]:
    """Each tool output the model saw as its own evidence, with the markers of its sources, so
    the verifier can check that a number cites the tool it came from."""
    out = []
    for tool in tools_seen:
        output = tool.get("output")
        if not isinstance(output, dict):
            continue
        markers = {int(m) for src in output.get("sources") or []
                   for m in re.findall(r"\d+", str(src.get("marker")))}  # fmt: skip
        if markers:
            evidence = build_evidence([{"args": tool.get("args"), "output": output}])
            out.append(Attributed(evidence, markers))
    return out


def _evidence(run: Run, now: datetime) -> Evidence:
    """What the model was shown (tool arguments and outputs), the question, the clock, and the
    tool descriptions (static reference text): everything a number in the answer may come from.
    A follow-up's earlier questions and answers (run.context) are deliberately left out: a figure
    from an earlier answer counts only if a tool of this run returned it again."""
    reference = " ".join(tool.description for tool in TOOLS.values())
    payloads = [{"args": t.get("args"), "output": t.get("output")} for t in run.tools]
    return build_evidence(payloads, question=run.question, now=now, extra=reference)


async def unseen_audit(api: InternalApi) -> dict[str, Any] | None:
    """The burn-scar audit of candidate unseen burns (/api/scars/summary method.audit.candidate_unseen):
    the share an audit found to be real burns, its 95% interval and how many were audited. Any
    candidate unseen share or area must come with it (verifier.check_honesty). The burn-scar tool
    now carries it, but stored runs and the bulletin's older tool outputs may not, so the bulletin
    and the answer check add it to that tool's output when it is missing. None if the audit is
    unavailable."""
    try:
        reply = await api.get("/api/scars/summary")
    except Exception as exc:  # the check then drops unqualified shares instead
        log.warning("burn-scar audit unavailable: %s", exc)
        return None
    return unseen_audit_block(reply.body) if reply.ok else None


async def _with_unseen_audit(api: InternalApi, run: Run) -> HonestyFacts:
    """The honesty facts of the run's tools; when they state candidate unseen burns without the
    audit, the audit is added to the burn-scar tool's output first (so the revision pass may quote
    it, the number check finds it, and the stored run shows where it came from)."""
    facts = honesty_facts([{"args": t.get("args"), "output": t.get("output")} for t in run.tools])
    if facts.precision is not None or not (facts.unseen_area_ha or facts.unseen_share_pct):
        return facts
    block = await unseen_audit(api)
    scars = [t for t in run.tools if t.get("name") == "get_burn_scars" and isinstance(t.get("output"), dict)]
    if block is None or not scars:
        return facts
    for tool in scars:
        tool["output"]["candidate_unseen_audit"] = block
    return honesty_facts([{"args": t.get("args"), "output": t.get("output")} for t in run.tools])


async def _evidence_pack(api: InternalApi, run: Run, book: CitationBook) -> None:
    """Gemini cannot answer: still show the evidence the agent would have used (no narrative)."""
    city = "Delhi"  # the main receptor; a city named in the question wins
    latest = await api.get("/api/air/latest")
    if latest.ok:
        for receptor in latest.body.get("receptors") or []:
            name = str(receptor.get("city") or "")
            if name and name.lower() in run.question.lower():
                city = name
                break
    plan = [
        ("get_air_quality", {"city": city}),
        ("trace_air_back", {"city": city}),
        ("get_fire_activity", {"hours": 24}),
        ("get_weather", {"city": city}),
    ]
    calls = [types.FunctionCall(id=f"evidence_{i + 1}", name=n, args=a)
             for i, (n, a) in enumerate(plan)]  # fmt: skip
    await _run_calls(api, run, book, calls)


async def investigate(run: Run, client_factory: ClientFactory | None = None) -> None:
    """Run one question to the end, emitting events on `run`. Never raises."""
    t0 = time.perf_counter()
    gateway = Gateway(run.kind, run.id, client_factory)
    book = CitationBook()
    api = InternalApi()
    now = utcnow()
    try:
        planned = await gateway.first_choice()
        run.emit("run", run_id=run.id, model=planned, lang=run.lang, **_follow_up_fields(run))
        # A follow-up's earlier questions and answers come first, as a part of their own marked as
        # context, and its system prompt adds the rule that they are not evidence. A question asked
        # on its own sends the same bytes as before (Gemini's implicit prompt cache).
        system = prompts.INVESTIGATOR_FOLLOW_UP_SYSTEM if run.context else prompts.INVESTIGATOR_SYSTEM
        first_turn = [types.Part.from_text(
            text=prompts.investigation_turn(run.question, run.lang, now))]  # fmt: skip
        if run.context:
            first_turn.insert(0, types.Part.from_text(text=prompts.context_turn(run.context)))
        contents = [types.Content(role="user", parts=first_turn)]
        model: str | None = None
        mode, retried_validated, rounds, restarts = "AUTO", False, 0, 0
        answer = ""
        try:
            while True:
                final = rounds >= MAX_ROUNDS
                config = _config(mode="NONE" if final else mode, thinking="MEDIUM", system=system)
                try:
                    turn, contents = await model_turn(gateway, run, contents, config,
                                                      prefer=model, stream_text=True)  # fmt: skip
                except StreamInterrupted as exc:
                    if exc.answer_shown or restarts >= 2:
                        raise
                    restarts += 1  # nothing of the answer shown: redo this turn elsewhere
                    log.warning("agent run %s: %s; retrying the turn", run.id, exc)
                    continue
                model = turn.model
                if turn.blocked:
                    raise GeminiUnavailable(f"the question was blocked ({turn.blocked})",
                                            retryable=False)  # fmt: skip
                if turn.content is None:
                    bad_call = turn.finish in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL",
                                               "TOO_MANY_TOOL_CALLS")  # fmt: skip
                    if bad_call and not retried_validated:
                        mode, retried_validated = "VALIDATED", True  # retry with schema checks
                        continue
                    raise GeminiUnavailable(f"the model returned no content ({turn.finish})")
                contents.append(turn.content)
                if not turn.calls:
                    answer = turn.text
                    break
                rounds += 1
                contents.append(types.Content(role="user",
                                              parts=await _run_calls(api, run, book, turn.calls)))  # fmt: skip
        except GeminiUnavailable as exc:
            if not run.tools:  # nothing gathered yet: show the evidence without the narrative
                await _evidence_pack(api, run, book)
                run.emit("error", message=f"AI narrative unavailable: {exc}. The evidence above "
                         "is live data from BlindSpot's tools.", retryable=exc.retryable)  # fmt: skip
            else:
                run.emit("error", message=f"The investigation stopped: {exc}",
                         retryable=exc.retryable)  # fmt: skip
            run.error = str(exc)
            return
        if not answer:
            run.emit("error", message="The model finished without an answer.", retryable=True)
            run.error = "empty answer"
            return

        report = verify(answer, _evidence(run, now), set(book.refs), attributed(run.tools))
        first_report = report
        revised = False
        # The honesty rule (verifier.check_honesty): candidate unseen burns named as candidates and
        # qualified by the audit precision, no raw risk scores, no single-pair AUC as the ranking's
        # accuracy, FIRMS detections not called crop fires.
        # The burn-scar audit is added to the scar tool's output when an unseen share needs it.
        facts = await _with_unseen_audit(api, run)
        overclaims = [i.message(facts) for i in check_honesty([answer], run.lang, facts)]
        first_overclaims = overclaims
        if not report.passed or overclaims:
            revise = prompts.revision_turn(report.unverified, run.lang, overclaims)
            if run.context:  # a number copied from an earlier answer must go (no tools now)
                revise += prompts.FOLLOW_UP_REVISION_NOTE
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=revise)]))
            try:
                config = _config(mode="NONE", thinking="LOW", system=system)
                turn, contents = await model_turn(gateway, run, contents, config,
                                                  prefer=model, stream_text=False)  # fmt: skip
                if turn.text:
                    second = verify(turn.text, _evidence(run, now), set(book.refs),
                                    attributed(run.tools))  # fmt: skip
                    again = [i.message(facts) for i in check_honesty([turn.text], run.lang, facts)]
                    if len(second.unverified) + len(again) <= len(report.unverified) + len(overclaims):
                        answer, report, model, revised = turn.text, second, turn.model, True
                        overclaims = again
            except GeminiUnavailable as exc:  # keep the first answer, flagged as unverified
                log.warning("revision pass unavailable: %s", exc)
        rewritten: list[str] = []
        if overclaims:  # still overclaiming after the revision: rewrite those sentences from the tools
            [answer], rewritten = honest_fallback([answer], run.lang, facts)
            report = verify(answer, _evidence(run, now), set(book.refs), attributed(run.tools))
        citations = [c for m in cited_markers(answer) if (c := book.citation(m))]
        run.answer, run.citations, run.model = answer, citations, model
        if (auto := _auto_map_event(run)) is not None:
            run.emit("map", **auto)
        run.verification = {**report.as_event(), "revised": revised,
                            "first_pass_unverified": first_report.unverified if revised else [],
                            "honesty": {"passed": not check_honesty([answer], run.lang, facts),
                                        "first_pass_issues": first_overclaims,
                                        "fallback_sentences": rewritten}}  # fmt: skip
        run.emit("answer", text=answer, citations=citations, model=model)
        run.emit("verification", **run.verification)
        run.ok = True
    except Exception as exc:  # the stream must always end cleanly
        log.exception("agent run %s failed", run.id)
        run.error = get_settings().redact(f"{type(exc).__name__}: {exc}")[:500]
        run.emit("error", message="The investigation failed unexpectedly.", retryable=True)
    finally:
        run.usage = [vars(c) for c in gateway.calls]
        run.duration_ms = round((time.perf_counter() - t0) * 1000)
        models = list(dict.fromkeys(c.model for c in gateway.calls if c.ok))
        run.emit("done", run_id=run.id, duration_ms=run.duration_ms, ok=run.ok, models=models)
        await gateway.aclose()
        await api.aclose()


# --- storage ----------------------------------------------------------------------------------


def save_run(run: Run) -> None:
    """Store the finished run in agent_run (insert or replace). Logs instead of raising."""
    from blindspot.db import session_scope
    from blindspot.models.agent import AgentRun

    try:
        with session_scope() as session:
            session.merge(
                AgentRun(
                    id=run.id,
                    kind=run.kind,
                    question=run.question,
                    lang=run.lang,
                    model=run.model,
                    started_at=run.started_at,
                    finished_at=utcnow(),
                    duration_ms=run.duration_ms,
                    events=run.events,
                    answer=run.answer,
                    citations=run.citations,
                    verification=run.verification,
                    ok=run.ok,
                    error=run.error,
                    cache_key=run.cache_key,
                    tools=run.tools,
                    usage=run.usage,
                )
            )
    except Exception:
        log.warning("could not store agent run %s", run.id, exc_info=True)


_RECENT_SQL = text(
    """
    SELECT id, model, started_at, duration_ms, events
    FROM agent_run
    WHERE cache_key = :key AND kind IN ('ask', 'featured') AND ok AND started_at >= :since
      AND jsonb_array_length(COALESCE(citations, '[]'::jsonb)) > 0  -- no evidence: do not replay
    ORDER BY started_at DESC
    LIMIT 1
    """
)


def find_recent(key: str, minutes: float) -> dict[str, Any] | None:
    from blindspot.db import SessionLocal

    try:
        with SessionLocal() as session:
            row = session.execute(
                _RECENT_SQL, {"key": key, "since": utcnow() - timedelta(minutes=minutes)}
            ).mappings().first()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("answer cache lookup failed: %s", exc)
        return None


# Stored answers this process found, so replaying one again (the same question asked over and over,
# by one client or many) reads its events (~15 KB) from Postgres at most once per REPLAY_MEMO_S per
# instance instead of on every request. A stored run never changes; the memo also ends with the
# run's own replay window.
REPLAY_MEMO_S = 120.0
_replay_memo: dict[str, tuple[float, dict[str, Any]]] = {}


def _stored_answer(key: str, minutes: float) -> dict[str, Any] | None:
    now = time.time()
    memo = _replay_memo.get(key)
    if memo is not None:
        if memo[0] > now:
            return memo[1]
        _replay_memo.pop(key, None)
    stored = find_recent(key, minutes)
    if stored is not None:
        started = stored.get("started_at")
        until = now + REPLAY_MEMO_S
        if isinstance(started, datetime):
            until = min(until, started.timestamp() + minutes * 60)
        if until > now:
            if len(_replay_memo) >= 256:
                _replay_memo.clear()
            _replay_memo[key] = (until, stored)
    return stored


def replay(stored: dict[str, Any]) -> Run:
    """A finished Run whose events are the stored ones, marked as a replay of that run."""
    run = Run(id=stored["id"], kind="ask", question="", lang="", started_at=stored["started_at"])
    for event in stored.get("events") or []:
        event = dict(event)
        if event.get("type") == "run" and "switched_from" not in event:
            event |= {"cached": True, "narrated_at": stored["started_at"]}
        if event.get("type") == "done":
            event |= {"replayed": True}
        run.events.append(event)
    run.finish()
    return run


# --- follow-up questions -------------------------------------------------------------------------
#
# Where the link is stored: in the run's first event (`run`), as `follow_up_of` and `context`, in
# agent_run.events. A new agent_run column would need a migration (init_db only creates missing
# tables, so the production table would lack it and every insert would fail), and run.tools is the
# model's evidence: the earlier answer must not be there.

MAX_CONTEXT_PAIRS = 2  # earlier question/answer pairs a follow-up carries: enough, and a small prompt
CONTEXT_ANSWER_CHARS = 1600  # answers are asked to stay under 220 words; a longer one is cut
FOLLOW_UP_KINDS = ("ask", "featured")  # investigations; a bulletin write-up is not one
# A citation marker as the verifier reads them ([3], [1, 2], [2-4]), with the spaces before it.
_MARKER = re.compile(r"[ \t]*\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]")


class FollowUpRejected(ValueError):
    """The run a follow-up names cannot be its context: unknown, not an investigation, without an
    answer, or too old. POST /api/agent/ask answers 400 with this message, before anything is
    counted against the client or sent to Gemini."""


class FollowUpUnavailable(RuntimeError):
    """The run a follow-up names could not be read (the database is unavailable): 503."""


def context_answer(answer: str) -> str:
    """An earlier answer as the model is shown it: without its citation markers, which number that
    run's sources (copied, "[4]" would point at another source of this run, or at none), and at
    most CONTEXT_ANSWER_CHARS long."""
    lines = (" ".join(line.split()) for line in _MARKER.sub("", answer).splitlines())
    text = "\n".join(line for line in lines if line)
    if len(text) > CONTEXT_ANSWER_CHARS:
        text = text[:CONTEXT_ANSWER_CHARS].rsplit(maxsplit=1)[0] + " …"
    return text


def follow_up_info(events: list[dict[str, Any]] | None) -> tuple[str | None, list[dict[str, str]]]:
    """From a run's events: the run it followed up and the context it was given, as its first
    `run` event recorded them; (None, []) for a question asked on its own."""
    head = next((e for e in events or [] if isinstance(e, dict) and e.get("type") == "run"), None)
    if head is None:
        return None, []
    parent = head.get("follow_up_of")
    pairs = [
        {"run_id": str(p.get("run_id") or ""), "question": p["question"], "answer": p["answer"]}
        for p in head.get("context") or []
        if isinstance(p, dict) and isinstance(p.get("question"), str)
        and isinstance(p.get("answer"), str)
    ]  # fmt: skip
    return (parent if isinstance(parent, str) else None), pairs


def follow_up_until(row: Any) -> datetime | None:
    """Until when an agent_run row can be followed up (follow_up_hours after it started), or None
    when it never can: only an investigation that produced an answer gives a follow-up context."""
    if row.kind not in FOLLOW_UP_KINDS or not row.ok or not row.answer or row.started_at is None:
        return None
    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)
    return started + timedelta(hours=agent_settings().follow_up_hours)


def follow_up_refusal(row: Any, run_id: str, now: datetime) -> str | None:
    """Why run `run_id` (its agent_run row, or None) cannot be followed up; None if it can."""
    if row is None:
        return (f"Cannot follow up {run_id}: no finished investigation has that id. Ask the full "
                "question instead.")  # fmt: skip
    if row.kind not in FOLLOW_UP_KINDS:
        return f"Cannot follow up {run_id}: it is a {row.kind} run, not an investigation."
    if not row.ok or not row.answer:
        return (f"Cannot follow up {run_id}: that investigation ended without an answer. Ask the "
                "full question instead.")  # fmt: skip
    until = follow_up_until(row)
    if until is not None and until <= now:
        return (f"Cannot follow up {run_id}: it is more than {agent_settings().follow_up_hours:g} h "
                "old and its data has moved on. Ask the full question again.")  # fmt: skip
    return None


_PARENT_SQL = text(
    """
    SELECT id, kind, ok, question, answer, started_at, events->0 AS head
    FROM agent_run
    WHERE id = :id
    """
)


def follow_up_context(run_id: str, now: datetime | None = None) -> list[dict[str, str]]:
    """The context a follow-up of run `run_id` is given: the context that run was given, then its
    own question and answer, oldest first and at most MAX_CONTEXT_PAIRS pairs, so a long thread
    keeps the prompt small. Only the answer text: no tool output of the earlier runs.

    Raises FollowUpRejected when that run cannot be followed up (follow_up_refusal) and
    FollowUpUnavailable when it cannot be read."""
    from blindspot.db import SessionLocal

    try:
        with SessionLocal() as session:
            row = session.execute(_PARENT_SQL, {"id": run_id}).first()
    except SQLAlchemyError as exc:
        log.warning("could not read run %s for a follow-up: %s", run_id, exc)
        raise FollowUpUnavailable(
            f"The investigation {run_id} could not be read right now (database unavailable). "
            "Try again shortly, or ask the full question."
        ) from exc
    refusal = follow_up_refusal(row, run_id, now or utcnow())
    if refusal is not None:
        raise FollowUpRejected(refusal)
    _, earlier = follow_up_info([row.head] if isinstance(row.head, dict) else [])
    own = {"run_id": row.id, "question": row.question, "answer": context_answer(row.answer)}
    return [*earlier, own][-MAX_CONTEXT_PAIRS:]


def _follow_up_fields(run: Run) -> dict[str, Any]:
    """What the `run` event records of a follow-up: the run it follows and the exact context."""
    return {"follow_up_of": run.follow_up_of, "context": run.context} if run.follow_up_of else {}


# --- entry points -----------------------------------------------------------------------------


class RateLimited(Exception):
    def __init__(self, retry_after_s: float, message: str) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class RateLimiter:
    """Sliding-window limits per client key (an IP address): N per minute and M per day.

    In process memory: exact for one long-lived process, the first line of defence everywhere.
    Serverless instances also count in the database (blindspot.models.client_quota, see ask())."""

    def __init__(self, per_minute: int, per_day: int, noun: str = "questions") -> None:
        self.per_minute, self.per_day, self.noun = per_minute, per_day, noun
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        # Clients the shared (database) count refused, until when: refused here without asking the
        # database again, so a client hammering past its limit costs no query per request.
        self._blocked: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def forget(self, key: str) -> None:
        """Take back the last hit of `key` (the shared count refused the request after all)."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()

    def block(self, key: str, retry_after_s: float, reason: str) -> None:
        """Remember that the shared count refused `key` for `retry_after_s` seconds."""
        with self._lock:
            self._blocked[key] = (time.time() + retry_after_s, reason)
            if len(self._blocked) > 10_000:
                now = time.time()
                for k in [k for k, (until, _) in self._blocked.items() if until <= now]:
                    del self._blocked[k]

    def _refusal(self, key: str, now: float) -> RateLimited | None:
        blocked = self._blocked.get(key)
        if blocked is not None:
            if blocked[0] > now:
                return RateLimited(blocked[0] - now, blocked[1])
            del self._blocked[key]
        hits = self._hits.get(key)
        if not hits:
            return None
        while hits and now - hits[0] > 86400:
            hits.popleft()
        last_minute = [t for t in hits if now - t <= 60]
        if len(last_minute) >= self.per_minute:
            return RateLimited(60 - (now - last_minute[0]) + 1, f"at most {self.per_minute} {self.noun} a minute")
        if len(hits) >= self.per_day:
            return RateLimited(86400 - (now - hits[0]) + 1, f"at most {self.per_day} {self.noun} a day")
        return None

    def peek(self, key: str) -> None:
        """Raise RateLimited if a hit for `key` would be refused here, without counting anything."""
        with self._lock:
            refusal = self._refusal(key, time.time())
        if refusal is not None:
            raise refusal

    def hit(self, key: str) -> None:
        """Count one request for `key`, or raise RateLimited without counting it."""
        now = time.time()
        with self._lock:
            refusal = self._refusal(key, now)
            if refusal is not None:
                raise refusal
            self._hits[key].append(now)
            if len(self._hits) > 10_000:  # forget idle clients
                for idle in [k for k, v in self._hits.items() if not v][:1000]:
                    del self._hits[idle]


_limiter: RateLimiter | None = None


def limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        s = agent_settings()
        _limiter = RateLimiter(s.ip_per_minute, s.ip_per_day)
    return _limiter


def count_question(client_key: str) -> None:
    """Count one new question from `client_key` or raise RateLimited (nothing counted then).

    First the in-memory limiter; then, on serverless deployments (client_quota.shared_limits_enabled),
    the same limits counted in Postgres across every instance, so a cold start or another instance
    does not reset them. Only new runs are counted: joins and replays never reach this."""
    from blindspot.models import client_quota

    mem = limiter()
    mem.hit(client_key)
    if not client_quota.shared_limits_enabled():
        return
    refusal = client_quota.hit_or_allow("ask", client_key, mem.per_minute, mem.per_day, mem.noun)
    if refusal is not None:
        mem.forget(client_key)
        mem.block(client_key, refusal.retry_after_s, refusal.reason)  # no query until then
        raise RateLimited(refusal.retry_after_s, refusal.reason)


async def _drive(run: Run, client_factory: ClientFactory | None) -> None:
    """Investigate, store the run, then close the stream: when a client sees the stream end,
    GET /api/agent/runs/{id} already has the run."""
    try:
        await investigate(run, client_factory)
    finally:
        await asyncio.to_thread(save_run, run)
        run.finish()
        if run.cache_key and _inflight.get(run.cache_key) is run:
            del _inflight[run.cache_key]


async def ask(
    question: str,
    lang: str | None,
    client_key: str,
    *,
    kind: str = "ask",
    client_factory: ClientFactory | None = None,
    follow_up_of: str | None = None,
) -> Run:
    """Start (or join, or replay) the investigation of `question`; return the Run to follow.

    Order: an identical question already running is joined; one answered within the cache window
    is replayed; otherwise the client's rate limit is checked and a new run starts in the
    background (it finishes and is stored even if the client disconnects).

    `follow_up_of` (an earlier run's id) makes it a follow-up: "identical" then also means the same
    earlier run (it is part of the cache key), and that run is checked and its context read before
    the client is counted, so FollowUpRejected or FollowUpUnavailable cost the client nothing.
    """
    question = " ".join(question.split())
    lang = lang or detect_lang(question)
    follow_up_of = (follow_up_of or "").strip().lower() or None  # run ids are lowercase uuids
    key = cache_key(question, lang, follow_up_of)
    if kind == "ask":
        running = _inflight.get(key)
        if running is not None:  # asked a moment ago: follow the same run
            return running
        stored = await asyncio.to_thread(_stored_answer, key, agent_settings().cache_minutes)
        if stored is not None:
            return replay(stored)
    context = await asyncio.to_thread(follow_up_context, follow_up_of) if follow_up_of else []
    if kind == "ask":
        running = _inflight.get(key)  # it may have started while we looked in the database
        if running is not None:
            return running
        await asyncio.to_thread(count_question, client_key)
        running = _inflight.get(key)  # started while we counted: join it (the count stands)
        if running is not None:
            return running
    run = Run(id=str(uuid.uuid4()), kind=kind, question=question, lang=lang, cache_key=key,
              follow_up_of=follow_up_of, context=context)  # fmt: skip
    if kind == "ask":
        _inflight[key] = run
    task = asyncio.create_task(_drive(run, client_factory), name=f"agent-run-{run.id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run


async def run_to_end(
    question: str,
    lang: str,
    *,
    kind: str,
    client_factory: ClientFactory | None = None,
    follow_up_of: str | None = None,
) -> Run:
    """Run one investigation to completion without SSE (jobs, scripts). With `follow_up_of`, a
    follow-up of that run (FollowUpRejected or FollowUpUnavailable as in ask())."""
    follow_up_of = (follow_up_of or "").strip().lower() or None
    context = await asyncio.to_thread(follow_up_context, follow_up_of) if follow_up_of else []
    run = Run(id=str(uuid.uuid4()), kind=kind, question=question, lang=lang,
              cache_key=cache_key(question, lang, follow_up_of), follow_up_of=follow_up_of,
              context=context)  # fmt: skip
    await _drive(run, client_factory)
    return run


__all__ = [
    "MAX_CONTEXT_PAIRS",
    "MAX_ROUNDS",
    "AgentSettings",
    "FollowUpRejected",
    "FollowUpUnavailable",
    "Gateway",
    "GeminiUnavailable",
    "RateLimited",
    "RateLimiter",
    "Run",
    "agent_settings",
    "ask",
    "cache_key",
    "context_answer",
    "count_question",
    "detect_lang",
    "follow_up_context",
    "follow_up_info",
    "follow_up_until",
    "limiter",
    "model_status",
    "normalise_question",
    "quota_day_start",
    "replay",
    "reset_gateway_state",
    "run_to_end",
    "used_today",
]
