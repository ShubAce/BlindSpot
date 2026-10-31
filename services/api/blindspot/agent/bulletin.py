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
from datetime import UTC, datetime
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
    unseen_audit,
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
    HonestyFacts,
    build_evidence,
    check_honesty,
    cited_markers,
    foreign_script,
    honest_fallback,
    honesty_facts,
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


async def add_unseen_audit(api: InternalApi, facts: dict[str, Any]) -> None:
    """Put the burn-scar audit (runner.unseen_audit: /api/scars/summary method.audit.candidate_unseen)
    into the burn-scar block: the share of candidate unseen burns that an audit found to be real
    burns, its 95% interval and how many were audited. The bulletin must give these whenever it
    gives the candidate unseen share; the agent's burn-scar tool leaves them out."""
    block = (facts.get("burn_scars_season") or {}).get("output")
    if not isinstance(block, dict):
        return
    audit = await unseen_audit(api)
    if audit is not None:
        block["candidate_unseen_audit"] = audit


def units(doc: dict[str, Any]) -> list[str]:
    """The texts of a bulletin in order: title, summary, each section body."""
    return [doc["title"], doc["summary"], *(s["body"] for s in doc["sections"])]


def with_units(doc: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    sections = [{"heading": s["heading"], "body": body}
                for s, body in zip(doc["sections"], texts[2:], strict=True) if body]
    return {**doc, "title": texts[0], "summary": texts[1], "sections": sections}


def honest(lang: str, doc: dict[str, Any], hf: HonestyFacts, first: list[str], retried: bool
           ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The last line of the honesty rule: whatever still overclaims after the retry is rewritten
    from the tool outputs (verifier.honest_fallback). Returns (doc, the report for verification)."""
    left = [i.message(hf) for i in check_honesty(units(doc), lang, hf)]
    replaced: list[str] = []
    if left:
        texts, replaced = honest_fallback(units(doc), lang, hf)
        doc = with_units(doc, texts)
    return doc, {"passed": not check_honesty(units(doc), lang, hf), "first_pass_issues": first,
                 "retried": retried, "after_retry_issues": left if retried else [],
                 "fallback_sentences": replaced}


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
    now: datetime, hf: HonestyFacts | None = None, first_issues: list[str] | None = None,
    retried: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """The stored content of one language: the checked text, its citations and its report.

    With `hf` the honesty rule is enforced too (see honest()): before the number check, and once
    more after it, in case the number check removed the sentence that qualified a share."""
    honesty = None
    if hf is not None:
        doc, honesty = honest(lang, doc, hf, first_issues or [], retried)
    first = checker.check(full_text(doc))
    removed: list[str] = []
    if not first.passed:
        doc, removed = checker.scrub(doc)
    if hf is not None and honesty is not None and check_honesty(units(doc), lang, hf):
        texts, gone = honest_fallback(units(doc), lang, hf)
        doc = with_units(doc, texts)
        honesty["fallback_sentences"] += gone
        honesty["passed"] = not check_honesty(units(doc), lang, hf)
    final = checker.check(full_text(doc))
    # Letters of another script, or U+FFFD where the model sent bytes that are not valid UTF-8.
    mixed = foreign_script(full_text(doc), lang) + [w for w in full_text(doc).split() if "\ufffd" in w]
    citations = [c for m in cited_markers(full_text(doc)) if (c := book.citation(m))]
    return {
        **doc,
        "citations": citations,
        "verification": {**final.as_event(), "first_pass_unverified": first.unverified,
                         "removed_sentences": removed, "script_ok": not mixed,
                         "foreign_script_words": mixed[:20],
                         **({"honesty": honesty} if honesty is not None else {})},  # fmt: skip
        "data_as_of": ist(now),
        "model": model,
    }


async def translate(
    lang: str, english: dict[str, Any], checker: Checker, book: CitationBook, gateway: Gateway,
    now: datetime, hf: HonestyFacts | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The checked English in `lang`: one call; if its script check or its honesty check fails,
    one more call on the other model of the chain. Returns the better of the two (flagged when
    neither is clean of other scripts; an overclaim left after the retry is rewritten, see honest())."""
    turn = prompts.translation_turn(lang, english)
    system = prompts.TRANSLATE_SYSTEMS.get(lang, prompts.TRANSLATE_SYSTEM)

    def issues(doc: dict[str, Any]) -> list[str]:
        return [i.message(hf) for i in check_honesty(units(doc), lang, hf)] if hf is not None else []

    used, text = await _generate(gateway, turn, system=system, thinking="LOW",
                                 temperature=prompts.TRANSLATE_TEMPERATURE)  # fmt: skip
    doc = parse(text)
    first = issues(doc)
    best = publish(lang, doc, checker, book, used, now, hf, first)
    if best["verification"]["script_ok"] and not first:
        return best
    other = next((m for m in gateway.chain if m != used), None)
    log.warning("bulletin %s from %s: other-script letters %s, honesty issues %d; asking %s", lang, used,
                best["verification"]["foreign_script_words"][:5], len(first), other or "it again")  # fmt: skip
    used, text = await _generate(gateway, turn, system=system, thinking="LOW", prefer=other,
                                 temperature=prompts.TRANSLATE_TEMPERATURE)  # fmt: skip
    doc = parse(text)
    again = publish(lang, doc, checker, book, used, now, hf, first, retried=True)
    if again["verification"].get("honesty"):
        again["verification"]["honesty"]["after_retry_issues"] = issues(doc)

    def badness(c: dict[str, Any]) -> tuple[int, int]:
        h = c["verification"].get("honesty") or {}
        return (len(c["verification"]["foreign_script_words"]), len(h.get("fallback_sentences") or []))

    if badness(again) <= badness(best):
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
        await add_unseen_audit(api, facts)
        hf = honesty_facts(list(facts.values()))
        day_label = f"{now.astimezone(IST):%A %d %B %Y}"
        turn = prompts.bulletin_turn(day_label, now, facts)
        model, raw = await _generate(gateway, turn, system=prompts.BULLETIN_SYSTEM, thinking="MEDIUM")
        draft = parse(raw)
        first = [i.message(hf) for i in check_honesty(units(draft), "en", hf)]
        retried = False
        if first:  # one retry with the rejected sentences; what still overclaims is rewritten in publish()
            try:
                again, raw2 = await _generate(gateway, turn + prompts.honesty_turn(first, draft),
                                              system=prompts.BULLETIN_SYSTEM, thinking="MEDIUM")  # fmt: skip
                second = parse(raw2)
                retried = True
                if len(check_honesty(units(second), "en", hf)) <= len(first):
                    draft, model = second, again
            except (GeminiUnavailable, ValueError, errors.APIError, httpx.HTTPError) as exc:
                log.warning("bulletin: the honesty retry failed (%s); rewriting from the facts", exc)
        english = publish("en", draft, Checker.for_facts(facts, book, now), book, model, now, hf, first, retried)
        published = {"en": english}
        same_numbers = Checker.for_translation(english, book, now)
        langs = [lang for lang in prompts.LANGUAGES if lang != "en"]
        outcomes = await asyncio.gather(
            *(translate(lang, english, same_numbers, book, gateway, now, hf) for lang in langs),
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


def _stored_facts(day: Any) -> HonestyFacts:
    """The honesty facts of the run that wrote the stored English (its tool outputs)."""
    from blindspot.db import SessionLocal
    from blindspot.models.agent import AgentRun, Bulletin

    with SessionLocal() as session:
        tools = session.execute(
            select(AgentRun.tools).join(Bulletin, Bulletin.run_id == AgentRun.id)
            .where(Bulletin.date == day, Bulletin.lang == "en")
        ).scalar_one_or_none()
    return honesty_facts([{"args": t.get("args"), "output": t.get("output")} for t in tools or []])


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
        hf = await asyncio.to_thread(_stored_facts, day)
        outcomes = await asyncio.gather(
            *(translate(lang, english, checker, book, gateway, now, hf) for lang in langs),
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


# --- the honesty rule on a bulletin already stored (no Gemini) ------------------------------------------


def _stored_day(day: Any) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """The stored bulletin rows of `day` ({lang: content}) and the run that wrote the English."""
    from blindspot.db import SessionLocal
    from blindspot.models.agent import AgentRun, Bulletin

    with SessionLocal() as session:
        rows = dict(session.execute(select(Bulletin.lang, Bulletin.content).where(Bulletin.date == day)).all())
        found = session.execute(
            select(AgentRun.id, AgentRun.started_at, AgentRun.tools, AgentRun.events)
            .join(Bulletin, Bulletin.run_id == AgentRun.id).where(Bulletin.date == day, Bulletin.lang == "en")
        ).first()
    run = None if found is None else {"id": found[0], "started_at": found[1], "tools": found[2] or [],
                                      "events": found[3] or []}
    return rows, run


def _save_recheck(day: Any, docs: dict[str, dict[str, Any]], run_id: str, tools: list[dict[str, Any]]) -> None:
    """Replace the content of the rewritten rows (each keeps its run and generated_at) and record the
    audit the check added in the English run's tools (where the numbers came from)."""
    from blindspot.db import session_scope
    from blindspot.models.agent import AgentRun, Bulletin

    with session_scope() as session:
        for lang, content in docs.items():
            session.execute(Bulletin.__table__.update().where(Bulletin.date == day, Bulletin.lang == lang)
                            .values(content=content))
        session.execute(AgentRun.__table__.update().where(AgentRun.id == run_id).values(tools=tools))


async def recheck_stored(day: Any, *, write: bool = True) -> dict[str, Any]:
    """Apply the honesty rule to the stored bulletin of `day` without calling Gemini: what overclaims
    is rewritten from the tool outputs of the run that wrote its English (verifier.honest_fallback),
    then the numbers are checked again exactly as on the first run (the English against those tool
    outputs and their markers, each translation against the new English). Rows that pass are left as
    they are. For bulletins written before the rule existed (the live one of 2026-10-31); with
    write=False nothing is stored and the summary says what would change."""
    from blindspot.provenance import SourceRef

    rows, run = await asyncio.to_thread(_stored_day, day)
    if "en" not in rows or run is None or not run["tools"]:
        raise RuntimeError(f"no stored English bulletin with its tool outputs for {day}")
    tools = run["tools"]
    facts = {str(t.get("id") or i): {k: v for k, v in t.items() if k not in ("id", "name", "args", "ok")}
             for i, t in enumerate(tools)}
    hf = honesty_facts(list(facts.values()))
    audit_added = False
    if hf.precision is None and (hf.unseen_area_ha or hf.unseen_share_pct):
        api = InternalApi(timeout_s=FACT_TIMEOUT_S)
        try:
            audit = await unseen_audit(api)
        finally:
            await api.aclose()
        for tool in tools:
            if audit is not None and tool.get("name") == "get_burn_scars" and isinstance(tool.get("output"), dict):
                tool["output"]["candidate_unseen_audit"] = audit  # the same dict as in `facts`
                audit_added = True
        hf = honesty_facts(list(facts.values()))
    book = CitationBook()  # every source the run saw, with its marker
    for event in run["events"]:
        for source in event.get("sources") or [] if event.get("type") == "tool_result" else []:
            book.refs[int(source["marker"])] = SourceRef.model_validate({k: v for k, v in source.items() if k != "marker"})
    for citation in rows["en"].get("citations") or []:
        book.refs.setdefault(int(citation["marker"]), SourceRef.model_validate(citation["source"]))
    now = run["started_at"]  # the clock of the first run: its "today", "yesterday" and "as of"
    if now.tzinfo is None:  # the codebase convention: naive means UTC
        now = now.replace(tzinfo=UTC)

    def doc_of(content: dict[str, Any]) -> dict[str, Any]:
        return {"title": content["title"], "summary": content["summary"], "sections": content["sections"]}

    def redo(lang: str, content: dict[str, Any], checker: Checker) -> dict[str, Any] | None:
        doc = doc_of(content)
        first = [i.message(hf) for i in check_honesty(units(doc), lang, hf)]
        if not first:
            return None
        new = publish(lang, doc, checker, book, content.get("model") or "", now, hf, first)
        new["data_as_of"] = content.get("data_as_of", new["data_as_of"])
        new["verification"]["honesty"]["rechecked_at"] = ist(utcnow())
        return new

    changed: dict[str, dict[str, Any]] = {}
    english = redo("en", rows["en"], Checker.for_facts(facts, book, now))
    if english is not None:
        changed["en"] = english
    same_numbers = Checker.for_translation(english or rows["en"], book, now)
    for lang in (lang for lang in prompts.LANGUAGES if lang != "en" and lang in rows):
        again = redo(lang, rows[lang], same_numbers)
        if again is not None:
            changed[lang] = again
    if write and changed:
        await asyncio.to_thread(_save_recheck, day, changed, run["id"], tools)
    return {
        "date": str(day), "written": bool(write and changed), "audit_added": audit_added,
        "languages": {lang: {"rewritten_sentences": len(c["verification"]["honesty"]["fallback_sentences"]),
                             "honesty_passed": c["verification"]["honesty"]["passed"],
                             "verified": c["verification"]["passed"], "script_ok": c["verification"]["script_ok"],
                             "removed_sentences": len(c["verification"]["removed_sentences"])}
                      for lang, c in changed.items()},
        "unchanged": sorted(set(rows) - set(changed)),
        "texts": {lang: full_text(c) for lang, c in changed.items()},
    }


if __name__ == "__main__":  # python -m blindspot.agent.bulletin recheck 2026-10-31 [--dry-run]
    import sys
    from datetime import date

    if len(sys.argv) < 3 or sys.argv[1] != "recheck":
        raise SystemExit("usage: python -m blindspot.agent.bulletin recheck YYYY-MM-DD [--dry-run]")
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    outcome = asyncio.run(recheck_stored(date.fromisoformat(sys.argv[2]), write="--dry-run" not in sys.argv))
    print(json.dumps(outcome, ensure_ascii=False, indent=1, default=str))
