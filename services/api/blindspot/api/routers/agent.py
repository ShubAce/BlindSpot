"""The investigator agent over HTTP.

POST /api/agent/ask        {question, lang?} -> text/event-stream of agent events
GET  /api/agent/runs/{id}  one stored run: events, answer, citations, verification, tool outputs
GET  /api/agent/examples   recent runs whose answers passed verification
GET  /api/agent/status     Gemini budget per model, cooldowns, rate limits (for the UI badge)

The ask route only starts (or joins, or replays) a run and streams its events; the run itself
lives in blindspot.agent.runner and finishes even if the browser disconnects.
"""

from __future__ import annotations

from typing import Any, Literal

import orjson
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sse_starlette import EventSourceResponse, ServerSentEvent

from blindspot.agent import runner
from blindspot.api import SessionDep, respond
from blindspot.config import get_settings
from blindspot.models.agent import AgentRun
from blindspot.models.client_quota import client_address, shared_limits_enabled
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch

router = APIRouter(prefix="/api/agent", tags=["agent"])

_JSON = orjson.OPT_UTC_Z | orjson.OPT_NAIVE_UTC | orjson.OPT_NON_STR_KEYS


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=600, description="The question, any language")
    lang: Literal["en", "hi", "pa"] | None = Field(
        None, description="Answer language; by default the script of the question decides"
    )

    @field_validator("question")
    @classmethod
    def _tidy(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("ask a question of at least 3 characters")
        return value


@router.post(
    "/ask",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}}, 429: {"description": "rate limited"}},
)
async def ask(body: AskBody, request: Request) -> EventSourceResponse:
    """Investigate a question. Streams `run`, `thought`, `tool_call`, `map`, `tool_result`,
    `answer_delta`, `answer`, `verification`, `error` and finally `done`, one JSON object per
    `data:` line (its `type` repeats the SSE event name)."""
    # The real client address (Vercel's forwarded address on Vercel, the socket peer elsewhere);
    # on serverless the limit is also counted in Postgres under a salted hash of it.
    client = client_address(request.headers, request.client.host if request.client else None)
    try:
        run = await runner.ask(body.question, body.lang, client)
    except runner.RateLimited as exc:
        wait = int(exc.retry_after_s) + 1
        raise HTTPException(  # the API's JSON error shape; Retry-After says when to come back
            status_code=429,
            detail=f"Too many questions from this address ({exc}). Try again in {wait} s.",
            headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
        ) from None

    async def events():  # noqa: ANN202 - async generator of SSE frames
        async for event in run.follow():
            yield ServerSentEvent(
                data=orjson.dumps(event, option=_JSON, default=_default).decode(),
                event=event["type"],
            )

    return EventSourceResponse(
        events(), ping=15, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def _sources_of(row: AgentRun, session: SessionDep) -> list[SourceRef]:
    """The run's cited sources plus the Gemini calls that wrote it."""
    refs: list[SourceRef] = []
    seen: set[tuple[Any, ...]] = set()
    for citation in row.citations or []:
        try:
            ref = SourceRef.model_validate(citation.get("source") or {})
        except ValueError:
            continue
        identity = (ref.key, ref.fetch_id, ref.url)
        if identity not in seen:
            seen.add(identity)
            refs.append(ref)
    for call in row.usage or []:
        ref = ref_for_fetch(session, call.get("fetch_id"))
        if ref is not None and (ref.key, ref.fetch_id, ref.url) not in seen:
            seen.add((ref.key, ref.fetch_id, ref.url))
            refs.append(ref)
    return refs


def _run_dict(row: AgentRun, *, full: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "kind": row.kind,
        "question": row.question,
        "lang": row.lang,
        "model": row.model,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "duration_ms": row.duration_ms,
        "ok": row.ok,
        "answer": row.answer,
        "citations": row.citations or [],
        "verification": row.verification,
        "tools_used": [t.get("name") for t in row.tools or []],
    }
    if full:
        out |= {"error": row.error, "events": row.events or [], "tools": row.tools or [],
                "usage": row.usage or []}  # fmt: skip
    return out


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: SessionDep):  # noqa: ANN201
    """One stored run, with everything the model was shown (`tools`) and every Gemini call."""
    row = session.get(AgentRun, run_id[:36])
    if row is None:
        raise HTTPException(status_code=404, detail="no agent run with that id")
    return respond({"run": _run_dict(row, full=True)}, _sources_of(row, session), max_age=60)


_EXAMPLES_SQL = text(
    """
    SELECT id FROM (
        SELECT DISTINCT ON (cache_key) id, started_at
        FROM agent_run
        WHERE kind IN ('ask', 'featured') AND ok AND answer IS NOT NULL
          AND (verification->>'passed')::boolean IS TRUE
          -- and the honesty check (candidate wording, audit, no "unobserved" claims...): runs made
          -- before it existed carry no honesty block and are never shown as verified examples
          AND (verification->'honesty'->>'passed')::boolean IS TRUE
          AND jsonb_array_length(COALESCE(citations, '[]'::jsonb)) > 0  -- cites evidence
          AND (CAST(:lang AS text) IS NULL OR lang = :lang)
        ORDER BY cache_key, started_at DESC
    ) latest
    ORDER BY started_at DESC
    LIMIT :limit
    """
)


@router.get("/examples")
def examples(  # noqa: ANN201
    session: SessionDep, limit: int = 6, lang: Literal["en", "hi", "pa"] | None = None
):
    """Recent investigations whose every number passed verification (newest first, one per
    distinct question)."""
    limit = min(max(limit, 1), 20)
    ids = session.execute(_EXAMPLES_SQL, {"limit": limit, "lang": lang}).scalars().all()
    rows = [row for i in ids if (row := session.get(AgentRun, i)) is not None]
    sources: list[SourceRef] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        for ref in _sources_of(row, session):
            if (ref.key, ref.fetch_id, ref.url) not in seen:
                seen.add((ref.key, ref.fetch_id, ref.url))
                sources.append(ref)
    return respond(
        {"examples": [_run_dict(row, full=False) for row in rows]}, sources, max_age=30
    )


@router.get("/status")
def status(session: SessionDep):  # noqa: ANN201
    """How much Gemini quota is left per model today, cooldowns, and the per-IP limits."""
    settings = runner.agent_settings()
    return respond(
        {
            "key_configured": get_settings().gemini_api_key is not None,
            "quota_day_started_at": runner.quota_day_start(),
            "reserve_share": settings.reserve,
            "models": runner.model_status(),
            "per_ip_limits": {"per_minute": settings.ip_per_minute, "per_day": settings.ip_per_day,
                              "shared_across_instances": shared_limits_enabled()},
            "answer_cache_minutes": settings.cache_minutes,
        },
        latest_refs(session, ["gemini"]),
        max_age=10,
    )
