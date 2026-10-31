"""The daily public bulletin, in English, Hindi and Punjabi, written from tool outputs only.

How it is written (the bulletin job calls write_bulletin() at 07:30 IST):
1. Facts: the agent's own tools run without the model (air quality, back-trajectories, weather,
   fires, smoke forecast, burn scars, burn risk), each block with its citation markers. If none of
   them has data, nothing is written: there is nothing true to say.
2. One Gemini call writes the English bulletin from the facts (structured JSON output).
3. The verifier checks every number of it against the facts, and that each number cites the
   source it came from. A sentence that fails is removed before publishing, and the removal is
   recorded.
4. One Gemini call per language translates the checked English into Hindi and into Punjabi (in
   parallel), keeping numbers and markers, at low temperature, under an explicit script rule
   (prompts.SCRIPT_RULES: Gurmukhi U+0A00-U+0A7F only for Punjabi). Each is checked the same way,
   plus a script check (no letter of another script; Gemini's free models have put Sinhala,
   Bengali and Cyrillic letters inside Punjabi words). A translation that fails the script check
   is asked once more, of the other model of the bulletin chain; if that fails too, the better of
   the two is stored flagged (verification.script_ok = false) and the API reports the language as
   unavailable.
5. One bulletin row per language (rewriting a day replaces it) and one agent_run row for audit.
So a bulletin costs three Gemini calls, four or five when a script check fails. A later run of the
day whose English is already written only translates the missing or flagged languages, from the
stored English (one or two calls), so the hourly retries do not rewrite what is already good.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
from google.genai import errors, types
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from blindspot.agent import prompts
from blindspot.agent.runner import (
    ClientFactory,
    Gateway,
    GeminiUnavailable,
    Run,
    attributed,
    cool_after,
    save_run,
)
from blindspot.agent.tools import (
    TOOLS,
    CitationBook,
    InternalApi,
    ToolResult,
    ist,
    model_payload,
    run_tool,
)
from blindspot.agent.verifier import (
    Attributed,
    Evidence,
    build_evidence,
    cited_markers,
    foreign_script,
    verify,
)
from blindspot.geo import IST
from blindspot.provenance import utcnow

log = logging.getLogger(__name__)

# What the bulletin is written from: (fact block name, tool, arguments). Delhi is the receptor
# city the product reports on; the fire, scar and risk tools cover all of Punjab and Haryana.
FACT_PLAN: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("air_quality_delhi", "get_air_quality", {"city": "Delhi", "hours": 24}),
    ("smoke_origin_delhi", "trace_air_back", {"city": "Delhi", "hours": 72}),
    ("weather_delhi", "get_weather", {"city": "Delhi"}),
    ("fires_last_24_hours", "get_fire_activity", {"hours": 24}),
    ("smoke_forecast", "forecast_smoke", {}),
    ("burn_scars_season", "get_burn_scars", {"limit": 5}),
    ("burn_risk", "get_burn_risk", {"limit": 5}),
)
_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
FACT_TIMEOUT_S = 60.0  # nobody waits on the morning job: give busy endpoints longer than a chat


async def gather_facts(
    api: InternalApi, book: CitationBook, run: Run
) -> tuple[dict[str, Any], int]:
    """Run every tool of FACT_PLAN in parallel. Returns ({block: payload}, blocks with data)."""
    for index, (label, name, args) in enumerate(FACT_PLAN):
        run.emit("tool_call", id=f"fact_{index + 1}", name=name, args=args, block=label)
    results: list[ToolResult] = await asyncio.gather(
        *(run_tool(api, name, args) for _, name, args in FACT_PLAN)
    )
    facts: dict[str, Any] = {}
    for index, ((label, name, args), result) in enumerate(zip(FACT_PLAN, results, strict=True)):
        payload = model_payload(result, book)
        facts[label] = payload
        run.tools.append({"id": f"fact_{index + 1}", "name": name, "args": args,
                          "ok": result.ok, **payload})  # fmt: skip
        run.emit("tool_result", id=f"fact_{index + 1}", name=name, ok=result.ok,
                 summary=result.summary if result.ok else f"{result.summary} ({result.error})",
                 sources=book.for_client(result.sources))  # fmt: skip
    return facts, sum(1 for r in results if r.ok)


async def _generate(
    gateway: Gateway, user_turn: str, *, system: str, thinking: str, prefer: str | None = None,
    temperature: float | None = None,
) -> tuple[str, str]:  # fmt: skip
    """One structured-output call, on `prefer` when it is available. Returns (model, JSON text).

    Gemini can send 503 "high demand" in the middle of a stream; the whole reply is read before
    it is used, so a broken stream is simply retried on the next model of the chain."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=prompts.BULLETIN_SCHEMA,  # not response_schema: see research s3
        thinking_config=types.ThinkingConfig(thinking_level=thinking),
        temperature=temperature,
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_turn)])]
    last: BaseException | None = None
    for _ in range(3):
        model, chunks, _sent = await gateway.stream(contents, config, prefer=prefer, priority=True)
        texts: list[str] = []
        try:
            async for chunk in chunks:
                if not chunk.candidates or not chunk.candidates[0].content:
                    continue
                for part in chunk.candidates[0].content.parts or []:
                    if part.text and not part.thought:
                        texts.append(part.text)
        except (errors.APIError, httpx.HTTPError) as exc:
            cool_after(model, exc)  # the gateway now skips this model
            log.warning("bulletin: the stream from %s broke (%s); trying the next model", model, exc)
            last = exc
            continue
        return model, "".join(texts)
    raise GeminiUnavailable(f"the Gemini stream kept breaking ({type(last).__name__}: {last})")


def parse(raw: str) -> dict[str, Any]:
    """The model's JSON as {title, summary, sections:[{heading, body}]}; ValueError if unusable."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the bulletin is not a JSON object")
    sections = [
        {"heading": str(s.get("heading") or "").strip(), "body": str(s.get("body") or "").strip()}
        for s in data.get("sections") or [] if isinstance(s, dict)
    ]  # fmt: skip
    doc = {
        "title": str(data.get("title") or "").strip(),
        "summary": str(data.get("summary") or "").strip(),
        "sections": [s for s in sections if s["body"]],
    }
    if not doc["title"] or not doc["sections"]:
        raise ValueError("the bulletin has no title or no sections")
    return doc


def full_text(doc: dict[str, Any]) -> str:
    parts = [doc["title"], doc["summary"]]
    for section in doc["sections"]:
        parts += [section["heading"], section["body"]]
    return "\n".join(parts)


class Checker:
    """Verifies bulletin text and removes what fails.

    The English text is checked against the facts (and each number against the source it
    cites). A translation is checked against the published English instead: it must carry the
    same numbers, which also catches "110.0" turning into "1100.0" or "120.0" into "12.0" even
    when the wrong number happens to appear somewhere else in the facts."""

    def __init__(
        self, evidence: Evidence, markers: set[int], by_source: list[Attributed] | None
    ) -> None:
        self.evidence, self.markers, self.by_source = evidence, markers, by_source

    @classmethod
    def for_facts(cls, facts: dict[str, Any], book: CitationBook, now: datetime) -> Checker:
        reference = " ".join(tool.description for tool in TOOLS.values())
        by_source = attributed([{"args": None, **payload} for payload in facts.values()])
        return cls(build_evidence([facts], now=now, extra=reference), set(book.refs), by_source)

    @classmethod
    def for_translation(cls, english: dict[str, Any], book: CitationBook, now: datetime) -> Checker:
        evidence = build_evidence([], now=now, extra=full_text(english))
        return cls(evidence, set(book.refs), None)

    def check(self, value: str) -> Any:
        return verify(value, self.evidence, self.markers, self.by_source)

    def _scrub_text(self, value: str) -> tuple[str, list[str]]:
        kept, removed = [], []
        for sentence in _SENTENCE.split(value):
            (kept if self.check(sentence).passed else removed).append(sentence)
        return " ".join(kept).strip(), removed

    def scrub(self, doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Remove every sentence that holds an unverifiable number; return what was removed."""
        summary, removed = self._scrub_text(doc["summary"])
        sections = []
        for section in doc["sections"]:
            body, gone = self._scrub_text(section["body"])
            removed += gone
            if body:
                sections.append({"heading": section["heading"], "body": body})
        title = doc["title"] if self.check(doc["title"]).passed else ""
        if not title:
            removed.append(doc["title"])
        return {"title": title, "summary": summary, "sections": sections}, removed


def publish(
    lang: str, doc: dict[str, Any], checker: Checker, book: CitationBook, model: str,
    now: datetime,
) -> dict[str, Any]:  # fmt: skip
    """The stored content of one language: the checked text, its citations and its report."""
    first = checker.check(full_text(doc))
    removed: list[str] = []
    if not first.passed:
        doc, removed = checker.scrub(doc)
    final = checker.check(full_text(doc))
    # Letters of another script, or U+FFFD where the model sent bytes that are not valid UTF-8.
    mixed = foreign_script(full_text(doc), lang) + [w for w in full_text(doc).split() if "\ufffd" in w]
    citations = [c for m in cited_markers(full_text(doc)) if (c := book.citation(m))]
    return {
        **doc,
        "citations": citations,
        "verification": {**final.as_event(), "first_pass_unverified": first.unverified,
                         "removed_sentences": removed, "script_ok": not mixed,
                         "foreign_script_words": mixed[:20]},  # fmt: skip
        "data_as_of": ist(now),
        "model": model,
    }


async def translate(
    lang: str, english: dict[str, Any], checker: Checker, book: CitationBook, gateway: Gateway,
    now: datetime,
) -> dict[str, Any]:  # fmt: skip
    """The checked English in `lang`: one call; if its script check fails, one more call on the
    other model of the chain. Returns the better of the two (flagged when neither is clean)."""
    turn = prompts.translation_turn(lang, english)
    system = prompts.TRANSLATE_SYSTEMS.get(lang, prompts.TRANSLATE_SYSTEM)
    used, text = await _generate(gateway, turn, system=system, thinking="LOW",
                                 temperature=prompts.TRANSLATE_TEMPERATURE)  # fmt: skip
    best = publish(lang, parse(text), checker, book, used, now)
    if best["verification"]["script_ok"]:
        return best
    other = next((m for m in gateway.chain if m != used), None)
    log.warning("bulletin %s: other-script letters from %s (%s); asking %s", lang, used,
                best["verification"]["foreign_script_words"][:5], other or "it again")  # fmt: skip
    used, text = await _generate(gateway, turn, system=system, thinking="LOW", prefer=other,
                                 temperature=prompts.TRANSLATE_TEMPERATURE)  # fmt: skip
    again = publish(lang, parse(text), checker, book, used, now)
    if len(again["verification"]["foreign_script_words"]) <= len(
        best["verification"]["foreign_script_words"]
    ):
        best = again
    if not best["verification"]["script_ok"]:
        log.warning("bulletin %s: still other-script letters (%s); stored flagged", lang,
                    best["verification"]["foreign_script_words"][:5])  # fmt: skip
    return best


def _written(day: Any) -> set[str]:
    from blindspot.db import SessionLocal
    from blindspot.models.agent import Bulletin

    with SessionLocal() as session:
        rows = session.execute(
            select(Bulletin.lang, Bulletin.content).where(Bulletin.date == day)
        ).all()
    # A language whose text mixes in another script is not done: the next hourly run rewrites it.
    return {lang for lang, content in rows
            if ((content or {}).get("verification") or {}).get("script_ok") is not False}  # fmt: skip


def _store(day: Any, docs: dict[str, dict[str, Any]], run_id: str) -> None:
    from blindspot.db import session_scope
    from blindspot.models.agent import Bulletin

    with session_scope() as session:
        for lang, content in docs.items():
            stmt = insert(Bulletin).values(
                date=day, lang=lang, generated_at=utcnow(), model=content.get("model"),
                content=content, run_id=run_id,
            )  # fmt: skip
            session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_bulletin_date_lang",
                    set_={"generated_at": func.now(), "model": stmt.excluded.model,
                          "content": stmt.excluded.content, "run_id": stmt.excluded.run_id},
                )  # fmt: skip
            )


async def write_bulletin(
    *, now: datetime | None = None, force: bool = False, client_factory: ClientFactory | None = None
) -> dict[str, Any]:
    """Write today's bulletin (IST date) unless all three languages exist. Returns a summary for
    job_run; raises when there is no data at all or Gemini cannot write it (the job retries)."""
    now = now or utcnow()
    day = now.astimezone(IST).date()
    written = set() if force else await asyncio.to_thread(_written, day)
    if written >= set(prompts.LANGUAGES):
        return {"skipped": f"the bulletin for {day} is already written", "date": str(day)}
    if "en" in written:  # only translations are missing (or flagged): translate the stored English
        missing = [lang for lang in prompts.LANGUAGES if lang not in written]
        return await translate_stored(day, missing, now=now, client_factory=client_factory)
    t0 = time.perf_counter()
    run = Run(id=str(uuid.uuid4()), kind="bulletin", question=f"Daily bulletin for {day}",
              lang="en")  # fmt: skip
    run.emit("run", run_id=run.id, model=None, lang="en")
    book = CitationBook()
    gateway = Gateway("bulletin", run.id, client_factory)
    api = InternalApi(timeout_s=FACT_TIMEOUT_S)
    try:
        facts, with_data = await gather_facts(api, book, run)
        if with_data == 0:
            reasons = "; ".join(str(v.get("error")) for v in facts.values())
            raise RuntimeError(f"no data to write a bulletin from ({reasons[:400]})")
        day_label = f"{now.astimezone(IST):%A %d %B %Y}"
        model, raw = await _generate(gateway, prompts.bulletin_turn(day_label, now, facts),
                                     system=prompts.BULLETIN_SYSTEM, thinking="MEDIUM")  # fmt: skip
        english = publish("en", parse(raw), Checker.for_facts(facts, book, now), book, model, now)
        published = {"en": english}
        same_numbers = Checker.for_translation(english, book, now)
        langs = [lang for lang in prompts.LANGUAGES if lang != "en"]
        outcomes = await asyncio.gather(
            *(translate(lang, english, same_numbers, book, gateway, now) for lang in langs),
            return_exceptions=True,
        )
        failures = {}
        for lang, outcome in zip(langs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures[lang] = f"{type(outcome).__name__}: {outcome}"[:300]
            else:
                published[lang] = outcome
        run.model, run.ok = model, not failures
        run.answer = full_text(published["en"])
        run.citations = published["en"]["citations"]
        run.verification = published["en"]["verification"]
        run.usage = [vars(c) for c in gateway.calls]
        await asyncio.to_thread(save_run, run)  # first: each bulletin row points at this run
        await asyncio.to_thread(_store, day, published, run.id)
        run.emit("answer", text=run.answer, citations=run.citations, model=model)
        run.emit("verification", **run.verification)
        summary = {
            "date": str(day),
            "languages": {lang: {"model": c["model"], "verified": c["verification"]["passed"],
                                 "removed_sentences": len(c["verification"]["removed_sentences"]),
                                 "script_ok": c["verification"]["script_ok"]}
                          for lang, c in published.items()},  # fmt: skip
            "fact_blocks_with_data": with_data,
            "gemini_calls": len(gateway.calls),
        }
        if failures:  # stored what worked; the job fails so the hourly retry writes all three
            run.error = f"translations failed: {failures}"
            raise RuntimeError(f"bulletin {day}: {run.error}; stored {sorted(published)}")
        return summary
    except Exception as exc:
        run.error = run.error or str(exc)[:500]
        run.emit("error", message=run.error, retryable=True)
        raise
    finally:
        run.usage = [vars(c) for c in gateway.calls]
        run.duration_ms = round((time.perf_counter() - t0) * 1000)
        run.emit("done", run_id=run.id, duration_ms=run.duration_ms, ok=run.ok)
        run.finish()
        await asyncio.to_thread(save_run, run)
        await gateway.aclose()
        await api.aclose()


def _stored_english(day: Any) -> dict[str, Any] | None:
    from blindspot.db import SessionLocal
    from blindspot.models.agent import Bulletin

    with SessionLocal() as session:
        return session.execute(
            select(Bulletin.content).where(Bulletin.date == day, Bulletin.lang == "en")
        ).scalar_one_or_none()


async def translate_stored(
    day: Any, langs: list[str], *, now: datetime | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Translate the stored, already verified English bulletin of `day` into `langs` (the ones
    missing or flagged), checked against that English exactly as on the first run."""
    from blindspot.provenance import SourceRef

    now = now or utcnow()
    english = await asyncio.to_thread(_stored_english, day)
    if not english or not english.get("sections"):
        raise RuntimeError(f"no stored English bulletin for {day} to translate")
    book = CitationBook()  # the English's own markers, so citations keep their numbers
    for citation in english.get("citations") or []:
        book.refs[int(citation["marker"])] = SourceRef.model_validate(citation["source"])
    t0 = time.perf_counter()
    run = Run(id=str(uuid.uuid4()), kind="bulletin", lang="en",
              question=f"Daily bulletin for {day}: {', '.join(langs)} from the stored English")  # fmt: skip
    run.emit("run", run_id=run.id, model=None, lang="en")
    gateway = Gateway("bulletin", run.id, client_factory)
    try:
        checker = Checker.for_translation(english, book, now)
        outcomes = await asyncio.gather(
            *(translate(lang, english, checker, book, gateway, now) for lang in langs),
            return_exceptions=True,
        )
        published, failures = {}, {}
        for lang, outcome in zip(langs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures[lang] = f"{type(outcome).__name__}: {outcome}"[:300]
            else:  # the facts are the English's: keep its "as of"
                published[lang] = {**outcome, "data_as_of": english.get("data_as_of", outcome["data_as_of"])}
        run.model = next((c["model"] for c in published.values()), None)
        run.ok = bool(published) and not failures
        run.answer = "\n\n".join(full_text(c) for c in published.values()) or None
        first = next(iter(published.values()), None)  # usually the one language that was missing
        run.citations = first["citations"] if first else []
        run.verification = first["verification"] if first else None
        run.usage = [vars(c) for c in gateway.calls]
        await asyncio.to_thread(save_run, run)  # first: each bulletin row points at this run
        if published:
            await asyncio.to_thread(_store, day, published, run.id)
        summary = {
            "date": str(day),
            "translated_from": "stored English",
            "languages": {lang: {"model": c["model"], "verified": c["verification"]["passed"],
                                 "removed_sentences": len(c["verification"]["removed_sentences"]),
                                 "script_ok": c["verification"]["script_ok"]}
                          for lang, c in published.items()},
            "gemini_calls": len(gateway.calls),
        }
        if failures:
            run.error = f"translations failed: {failures}"
            raise RuntimeError(f"bulletin {day}: {run.error}; stored {sorted(published)}")
        return summary
    except Exception as exc:
        run.error = run.error or str(exc)[:500]
        run.emit("error", message=run.error, retryable=True)
        raise
    finally:
        run.usage = [vars(c) for c in gateway.calls]
        run.duration_ms = round((time.perf_counter() - t0) * 1000)
        run.emit("done", run_id=run.id, duration_ms=run.duration_ms, ok=run.ok)
        run.finish()
        await asyncio.to_thread(save_run, run)
        await gateway.aclose()
