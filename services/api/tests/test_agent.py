"""Tests of the agent module: verifier, tools, runner loop, SSE routes, bulletin.

Gemini is never called here. The loop tests replay a recorded real Gemini stream
(tests/fixtures/agent/gemini_run_degraded.json, recorded 2026-10-30 from gemini-3.5-flash-lite)
and a recorded real Gemini 404 (gemini_error_404.json). Tool trimming is tested on recorded real
responses of our own air endpoints (endpoints_air.json). Rows a test creates are deleted by id.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from google.genai import errors, types
from sqlalchemy import text

from blindspot.agent import runner, tools
from blindspot.agent.verifier import build_evidence, cited_markers, extract_claims, verify
from blindspot.db import init_db, session_scope
from blindspot.provenance import utcnow

FIXTURES = Path(__file__).parent / "fixtures" / "agent"
RECORDED = json.loads((FIXTURES / "gemini_run_degraded.json").read_text(encoding="utf-8"))
ERROR_404 = json.loads((FIXTURES / "gemini_error_404.json").read_text(encoding="utf-8"))
AIR = json.loads((FIXTURES / "endpoints_air.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


@pytest.fixture(autouse=True)
def _fresh_gateway() -> None:
    runner.reset_gateway_state()
    runner._limiter = None


@pytest.fixture
def created() -> Any:
    """Collects run ids; afterwards deletes exactly the rows those runs created."""
    started = utcnow()
    run_ids: list[str] = []
    yield run_ids
    with session_scope() as session:
        for run_id in run_ids:
            session.execute(text("DELETE FROM bulletin WHERE run_id = :id"), {"id": run_id})
            session.execute(text("DELETE FROM agent_run WHERE id = :id"), {"id": run_id})
            session.execute(
                text("DELETE FROM source_fetch WHERE source_key = 'gemini' "
                     "AND meta->>'run_id' = :id"), {"id": run_id})  # fmt: skip
        # Place lookups by our tools use a source key only this module writes.
        session.execute(
            text("DELETE FROM source_fetch WHERE source_key = 'openmeteo_geocoding' "
                 "AND started_at >= :t"), {"t": started})  # fmt: skip


class ReplayClient:
    """Duck-types google-genai's async client: serves recorded real chunks, one recorded call
    per request, in order. `fail_first` exceptions are raised by the first requests instead."""

    def __init__(
        self,
        calls: list[dict[str, Any]],
        fail_first: list[Exception] | None = None,
        break_first: list[Exception] | None = None,
    ):
        self._calls = list(calls)
        self._fail = list(fail_first or [])
        self._break = list(break_first or [])  # raised after the first chunk of a request
        self.requests: list[dict[str, Any]] = []
        self.aio = self
        self.models = self

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        if self._fail:
            raise self._fail.pop(0)
        broken = self._break.pop(0) if self._break else None
        recorded = self._calls[0] if broken else self._calls.pop(0)
        chunks = [types.GenerateContentResponse.model_validate_json(c) for c in recorded["chunks"]]

        async def stream() -> Any:
            for i, chunk in enumerate(chunks):
                if broken is not None and i == 1:
                    raise broken
                yield chunk

        return stream()

    async def aclose(self) -> None:
        return None

    def close(self) -> None:
        return None


def _recorded_call_ids() -> list[str]:
    return [e["id"] for e in RECORDED["events"] if e["type"] == "tool_call"]


def _gemini_rows(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.execute(
            text("SELECT ok, http_status, meta FROM source_fetch WHERE source_key = 'gemini' "
                 "AND meta->>'run_id' = :id ORDER BY id"), {"id": run_id})  # fmt: skip
        return [dict(r._mapping) for r in rows]


# --- verifier ---------------------------------------------------------------------------------


NOW = datetime(2026, 10, 30, 17, 40, tzinfo=UTC)  # 23:10 IST


def test_extracts_numbers_units_times_and_dates() -> None:
    text_en = (
        "Delhi's PM2.5 was 72.5 µg/m³ [1] at 22:00 IST; NOAA-20 and Sentinel-2A saw 1,497 fires "
        "[2] since 15 September. 42% [3] of the air crossed the belt. Wind 12 km/h at 925 hPa.\n"
        "1. Fire activity may rise; 25 may be missed. Peak at 1:16 pm IST on Oct 31."
    )
    claims = [(c.kind, c.text) for c in extract_claims(text_en)]
    assert ("number", "72.5 µg/m³") in claims
    assert ("time", "22:00 IST") in claims
    assert ("number", "1,497") in claims
    assert ("date", "15 September.") in claims
    assert ("number", "42%") in claims
    assert ("number", "12 km/h") in claims
    assert ("time", "1:16 pm IST") in claims
    assert ("date", "Oct 31") in claims
    assert ("number", "25") in claims  # "25 may be missed": a number, not 25 May
    texts = [claim_text for _, claim_text in claims]
    for name_not_claim in ("2.5", "20", "2", "925", "1"):  # PM2.5, NOAA-20, S2A, hPa, list
        assert name_not_claim not in texts


def test_hindi_and_punjabi_digits_and_months() -> None:
    hindi = "दिल्ली में पीएम 2.5 का स्तर ७२.५ µg/m³ [1] था, दोपहर 1:20 बजे। 30 अक्टूबर को 1,23,456 हेक्टेयर।"
    claims = {(c.kind, c.value, c.unit, (c.day, c.month)) for c in extract_claims(hindi)}
    assert ("number", 72.5, "ugm3", (None, None)) in claims
    assert ("number", 123456.0, "ha", (None, None)) in claims
    assert ("date", None, None, (30, 10)) in claims
    punjabi = "ਦਿੱਲੀ ਵਿੱਚ PM2.5 ੭੨.੫ [1] ਸੀ; 42 ਪ੍ਰਤੀਸ਼ਤ [3] ਹਵਾ।"
    values = {(c.value, c.unit) for c in extract_claims(punjabi)}
    assert values == {(72.5, None), (42.0, "%")}


def test_verify_matches_rounding_units_times_and_flags_inventions() -> None:
    evidence = build_evidence(
        [{"pm25": 72.5, "fires": 1497, "share": {"h48": 0.42}, "wind": {"speed_ms": 3.4},
          "area_ha": 1250.0, "t": "2026-10-30T07:46:00Z", "obs": "2026-10-30 22:00 IST"}],
        question="Why?", now=NOW,
    )  # fmt: skip
    answer = (
        "PM2.5 was 72.5 µg/m³ [1] at 22:00 IST [1]. About 1,500 fires [2] were seen at "
        "1:16 pm IST [2]; 42% [3] of air came over the belt. Wind 12 km/h [4]; 12.5 km² burnt [5]. "
        "That is 5 times the WHO limit and 312 fires [2] in Sangrur."
    )
    report = verify(answer, evidence, known_markers={1, 2, 3, 4, 5})
    assert report.unverified[0].startswith("5 ")  # a computed ratio
    assert report.unverified[1].startswith("312 ")  # an invented count
    assert len(report.unverified) == 2 and not report.passed
    assert report.checked == report.verified + 2


def test_marker_right_after_a_plain_number_is_checked() -> None:
    """Live run 2026-10-31: "AQI of 92 [13]" was listed as uncited because the claim swallowed
    the marker, which also skipped the check that [13] is the source holding 92."""
    from blindspot.agent.verifier import Attributed

    now = utcnow()
    air, wind = build_evidence([{"aqi": 92}], now=now), build_evidence([{"kmh": 5.6}], now=now)
    both = build_evidence([{"aqi": 92, "kmh": 5.6}], now=now)
    by = [Attributed(air, {13}), Attributed(wind, {18})]
    good = verify("Delhi recorded a CPCB AQI of 92 [13].", both, {13, 18}, by)
    assert good.passed and good.uncited == []
    assert not verify("Delhi recorded a CPCB AQI of 92 [18].", both, {13, 18}, by).passed


def test_verify_flags_markers_without_a_source() -> None:
    evidence = build_evidence([{"x": 5}], now=NOW)
    report = verify("Five is 5 [1], see [9].", evidence, known_markers={1})
    assert report.bad_markers == [9]
    assert "[9] cites no source" in report.unverified
    assert cited_markers("a [2][5] b [1, 3] c [6-8]") == [2, 5, 1, 3, 6, 7, 8]


def test_recorded_real_answer_has_no_numbers_and_passes() -> None:
    answer = next(e for e in RECORDED["events"] if e["type"] == "answer")["text"]
    report = verify(answer, build_evidence([], now=NOW), known_markers=set())
    assert report.passed and report.checked == 0


# --- tools ------------------------------------------------------------------------------------


def _recorded_air_app() -> FastAPI:
    """Serves recorded real responses of our air endpoints (and nothing else)."""
    app = FastAPI()

    @app.get("/api/air/latest")
    def latest() -> JSONResponse:
        return JSONResponse(AIR["/api/air/latest"])

    @app.get("/api/air/stubble-share")
    def share() -> JSONResponse:
        return JSONResponse(AIR["/api/air/stubble-share"])

    return app


def test_air_tool_trims_recorded_real_payload() -> None:
    async def go() -> tuple[tools.ToolResult, tools.ToolResult]:
        async with tools.InternalApi(app=_recorded_air_app()) as api:
            return (await tools.get_air_quality(api, {"city": "delhi"}),
                    await tools.trace_air_back(api, {"city": "Delhi"}))  # fmt: skip

    air, back = asyncio.run(go())
    delhi = next(r for r in AIR["/api/air/latest"]["receptors"] if r["city"] == "Delhi")
    assert air.ok and air.data["city"] == "Delhi"
    assert air.data["now"]["pm25"] == delhi["pm25_now"]
    assert air.data["cpcb_aqi_now"]["aqi"] == delhi["aqi"]["value"]
    assert air.data["now"]["observed"].endswith(" IST")
    assert "last_48_hours" not in air.data  # this app has no /api/air/series: block omitted
    keys = {ref.key for ref in air.sources}
    assert {"naaqs_2009", "who_aqg_2021"} <= keys and keys & {"openaq", "datagov_aqi"}
    # /api/smoke/back is absent from this app: the tool still answers from IITM DSS, honestly.
    assert back.ok and "error" in back.data["back_trajectories"]
    dss = AIR["/api/air/stubble-share"]
    assert back.data["iitm_dss_official_estimate"]["run_date"] == dss["run_date"]


def test_tools_say_exactly_what_is_missing() -> None:
    async def go() -> tools.ToolResult:
        async with tools.InternalApi(app=FastAPI()) as api:  # an app with no routes at all
            return await tools.run_tool(api, "get_fire_activity", {"hours": 24})

    result = asyncio.run(go())
    assert not result.ok and result.data == {}
    assert "/api/fires returned HTTP 404" in (result.error or "")
    assert tools.model_payload(result, tools.CitationBook()) == {"error": result.error}


def test_show_on_map_validates_and_builds_payloads() -> None:
    async def go(args: dict[str, Any]) -> tools.ToolResult:
        async with tools.InternalApi(app=FastAPI()) as api:
            return await tools.run_tool(api, "show_on_map", args)

    fly = asyncio.run(go({"action": "fly_to", "lon": 75.84, "lat": 30.25, "zoom": 30}))
    assert fly.map_event == {"action": "fly_to",
                             "payload": {"center": [75.84, 30.25], "zoom": 16.0}}  # fmt: skip
    layer = asyncio.run(go({"action": "show_layer", "layer": "smoke_back", "city": "Delhi"}))
    assert layer.map_event["payload"] == {"layer": "smoke_back", "visible": True,
                                          "params": {"city": "Delhi"}}  # fmt: skip
    hl = asyncio.run(go({"action": "highlight", "feature_type": "scar", "feature_id": "17"}))
    assert hl.map_event["payload"] == {"feature_type": "scar", "id": "17"}
    assert not asyncio.run(go({"action": "show_layer", "layer": "nope"})).ok
    assert "outside" in (asyncio.run(go({"action": "fly_to", "lon": 10, "lat": 10})).error or "")


def test_helpers() -> None:
    assert tools.compass(315) == "NW" and tools.compass(0) == "N" and tools.compass(None) is None
    assert tools.naqi_band(72.5) == "Moderate" and tools.naqi_band(251) == "Severe"
    assert tools.ist("2026-10-30T07:46:00Z") == "2026-10-30 13:16 IST"
    assert str(tools.season_start(datetime(2026, 10, 30).date())) == "2026-09-15"
    assert str(tools.season_start(datetime(2026, 3, 1).date())) == "2025-09-15"
    book = tools.CitationBook()
    refs = tools.ApiReply("/x", 200, AIR["/api/air/latest"]).sources()
    first = [book.mark(r) for r in refs]
    assert first == list(range(1, len(refs) + 1)) and [book.mark(r) for r in refs] == first


# --- runner -----------------------------------------------------------------------------------


def _give_recorded_citations(run_id: str) -> None:
    """Attach the citations of the recorded real end-to-end run to a stored test run, so the
    cache and examples (which skip runs that cite nothing) treat it as an evidenced answer."""
    e2e = json.loads((FIXTURES / "gemini_run_e2e.json").read_text(encoding="utf-8"))
    cites = next(e["citations"] for e in e2e["events"] if e["type"] == "answer")
    with session_scope() as session:
        session.execute(text("UPDATE agent_run SET citations = CAST(:c AS jsonb) WHERE id = :i"),
                        {"c": json.dumps(cites), "i": run_id})  # fmt: skip


def _run(question: str, client: ReplayClient, created: list[str]) -> runner.Run:
    run = asyncio.run(runner.run_to_end(question, "en", kind="ask", client_factory=lambda: client))
    created.append(run.id)
    return run


def test_replayed_real_run_streams_the_contract(created: list[str]) -> None:
    client = ReplayClient(RECORDED["calls"])
    run = _run(f"{RECORDED['question']} [{uuid.uuid4().hex[:6]}]", client, created)
    types_seen = [e["type"] for e in run.events]
    assert types_seen[0] == "run" and run.events[0]["run_id"] == run.id
    assert types_seen[-3:] == ["answer", "verification", "done"]
    calls = [e for e in run.events if e["type"] == "tool_call"]
    results = {e["id"]: e for e in run.events if e["type"] == "tool_result"}
    assert [c["id"] for c in calls] == _recorded_call_ids()
    assert set(results) == {c["id"] for c in calls}  # every call answered once
    assert "thought" in types_seen
    answer = next(e for e in run.events if e["type"] == "answer")
    deltas = "".join(e["text"] for e in run.events if e["type"] == "answer_delta")
    assert deltas.strip() == answer["text"]
    assert run.verification["passed"] and run.ok
    # Gemini saw its own turns verbatim (thought signature included) and one response per call.
    second = client.requests[1]["contents"]
    assert [c.role for c in second] == ["user", "model", "user"]
    first_chunk = types.GenerateContentResponse.model_validate_json(RECORDED["calls"][0]["chunks"][0])
    signature = first_chunk.candidates[0].content.parts[0].thought_signature
    assert second[1].parts[0].thought_signature == signature
    responses = [p.function_response for p in second[2].parts]
    assert [r.id for r in responses] == _recorded_call_ids()[:3]
    assert all("error" in r.response or "output" in r.response for r in responses)
    # Stored, and every Gemini call is in source_fetch (which also feeds the daily budget).
    with session_scope() as session:
        stored = session.execute(text("SELECT ok, events, cache_key FROM agent_run WHERE id = :i"),
                                 {"i": run.id}).one()  # fmt: skip
    assert stored.ok and len(stored.events) == len(run.events) and stored.cache_key
    rows = _gemini_rows(run.id)
    assert len(rows) == 5 and all(r["ok"] for r in rows)
    assert {r["meta"]["model"] for r in rows} == {"gemini-3.5-flash-lite"}


def test_falls_back_after_recorded_real_404(created: list[str], monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENT_CHAIN", f"{ERROR_404['model']},gemini-3.5-flash-lite")
    not_found = errors.ClientError(ERROR_404["code"], ERROR_404["details"])
    client = ReplayClient(RECORDED["calls"], fail_first=[not_found])
    run = _run(f"fallback test {uuid.uuid4().hex[:6]}", client, created)
    assert [r["model"] for r in client.requests[:2]] == [ERROR_404["model"], "gemini-3.5-flash-lite"]
    switches = [e for e in run.events if e["type"] == "run"]
    assert switches[0]["model"] == ERROR_404["model"]
    assert switches[1] == {"type": "run", "run_id": run.id, "model": "gemini-3.5-flash-lite",
                           "switched_from": ERROR_404["model"]}  # fmt: skip
    assert run.ok
    status = {m["model"]: m for m in runner.model_status()}
    assert "404" in (status[ERROR_404["model"]]["cooldown_reason"] or "")
    rows = _gemini_rows(run.id)
    assert rows[0]["http_status"] == 404 and not rows[0]["ok"] and len(rows) == 6


def test_without_gemini_the_evidence_is_still_shown(created: list[str]) -> None:
    def no_key() -> Any:
        raise runner.GeminiUnavailable("GEMINI_API_KEY is not set", retryable=False)

    run = asyncio.run(runner.run_to_end(f"evidence test {uuid.uuid4().hex[:6]}", "en",
                                        kind="ask", client_factory=no_key))  # fmt: skip
    created.append(run.id)
    names = [e["name"] for e in run.events if e["type"] == "tool_call"]
    assert names == ["get_air_quality", "trace_air_back", "get_fire_activity", "get_weather"]
    error = next(e for e in run.events if e["type"] == "error")
    assert error["message"].startswith("AI narrative unavailable") and not error["retryable"]
    assert run.events[-1]["type"] == "done" and not run.ok and run.answer is None
    assert not any(e["type"] in ("answer", "answer_delta") for e in run.events)


def test_same_question_is_replayed_and_concurrent_ones_share_a_run(created: list[str]) -> None:
    question = f"cache test {uuid.uuid4().hex[:6]}"

    async def go() -> tuple[runner.Run, runner.Run, runner.Run]:
        client = ReplayClient(RECORDED["calls"])
        first, twin = await asyncio.gather(
            runner.ask(question, "en", "10.0.0.1", client_factory=lambda: client),
            runner.ask(question, "en", "10.0.0.2", client_factory=lambda: client),
        )
        async for _ in first.follow():
            pass
        # The recorded run had every tool down, so it cites nothing: not worth replaying.
        assert runner.find_recent(runner.cache_key(question, "en"), 10) is None
        _give_recorded_citations(first.id)
        again = await runner.ask(f"  {question.upper()}?? ", "en", "10.0.0.3")
        return first, twin, again

    first, twin, again = asyncio.run(go())
    created.append(first.id)
    assert twin is first  # single flight: one Gemini run for two simultaneous askers
    assert again.id == first.id and again.finished
    assert again.events[0]["cached"] is True and again.events[-1]["replayed"] is True
    assert [e["type"] for e in again.events] == [e["type"] for e in first.events]


def test_budget_keeps_a_reserve_and_limits_per_ip(monkeypatch: Any) -> None:
    monkeypatch.setattr(runner, "used_today", lambda: {"gemini-3.5-flash-lite": 400})
    ask = runner.Gateway("ask", "x", client_factory=lambda: None)
    bulletin = runner.Gateway("bulletin", "x", client_factory=lambda: None)
    used = runner.used_today()
    assert ask._over_budget("gemini-3.5-flash-lite", used, priority=False)  # 400 >= 80% of 500
    assert not ask._over_budget("gemini-3.5-flash-lite", used, priority=True)  # a run under way
    assert not bulletin._over_budget("gemini-3.5-flash-lite", used, priority=False)
    assert asyncio.run(ask.first_choice()) == "gemini-3.1-flash-lite"
    limiter = runner.RateLimiter(per_minute=2, per_day=5)
    limiter.hit("a")
    limiter.hit("a")
    with pytest.raises(runner.RateLimited):
        limiter.hit("a")
    limiter.hit("b")  # another address is not affected


def test_quota_day_and_question_normalisation() -> None:
    start = runner.quota_day_start(datetime(2026, 10, 30, 17, 40, tzinfo=UTC))
    assert start == datetime(2026, 10, 30, 7, 0, tzinfo=UTC)  # midnight PDT
    assert runner.normalise_question("Why is DELHI's air bad?? ") == runner.normalise_question(
        "why is delhi s air bad"
    )
    assert runner.detect_lang("दिल्ली की हवा") == "hi" and runner.detect_lang("ਦਿੱਲੀ") == "pa"
    assert runner.detect_lang("Delhi") == "en"


def test_neutralise_swaps_only_function_call_signatures() -> None:
    chunk = types.GenerateContentResponse.model_validate_json(RECORDED["calls"][0]["chunks"][0])
    content = chunk.candidates[0].content
    out = runner._neutralise([types.Content(role="user", parts=[types.Part.from_text(text="q")]),
                              content])  # fmt: skip
    assert out[1].parts[0].thought_signature == runner.SKIP_SIGNATURE
    assert content.parts[0].thought_signature != runner.SKIP_SIGNATURE  # the original is intact


# --- HTTP -------------------------------------------------------------------------------------


def _sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        event = next((ln[6:].strip() for ln in lines if ln.startswith("event:")), None)
        data = [ln[5:].strip() for ln in lines if ln.startswith("data:")]
        if event and data:
            assert len(data) == 1  # one JSON object per data: line
            frames.append((event, json.loads(data[0])))
    return frames


def test_ask_streams_sse_and_the_run_is_readable(created: list[str], monkeypatch: Any) -> None:
    from blindspot.api.main import app

    monkeypatch.setattr(runner, "default_client_factory", lambda: ReplayClient(RECORDED["calls"]))
    question = f"Why is Delhi's air bad today? {uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        response = client.post("/api/agent/ask", json={"question": question, "lang": "en"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = _sse(response.text)
        assert all(name == data["type"] for name, data in frames)
        run_id = frames[0][1]["run_id"]
        created.append(run_id)
        assert frames[-1][0] == "done"
        stored = client.get(f"/api/agent/runs/{run_id}").json()
        assert stored["run"]["id"] == run_id and stored["generated_at"].endswith("Z")
        assert [e["type"] for e in stored["run"]["events"]] == [n for n, _ in frames]
        assert any(s["key"] == "gemini" for s in stored["sources"])
        examples = client.get("/api/agent/examples?limit=20").json()
        assert run_id not in [x["id"] for x in examples["examples"]]  # cites nothing
        _give_recorded_citations(run_id)
        examples = client.get("/api/agent/examples?limit=20").json()
        assert run_id in [x["id"] for x in examples["examples"]] and "sources" in examples
        # a run from before the honesty check (no honesty block) is never a verified example
        with session_scope() as session:
            session.execute(text("UPDATE agent_run SET verification = verification - 'honesty' WHERE id = :i"),
                            {"i": run_id})  # fmt: skip
        examples = client.get("/api/agent/examples?limit=20").json()
        assert run_id not in [x["id"] for x in examples["examples"]]
        assert client.get("/api/agent/runs/no-such-run").status_code == 404
        status = client.get("/api/agent/status").json()
        assert status["key_configured"] in (True, False) and status["models"]


def test_ask_validates_and_rate_limits(monkeypatch: Any, created: list[str]) -> None:
    from blindspot.api.main import app

    monkeypatch.setattr(runner, "default_client_factory", lambda: ReplayClient(RECORDED["calls"]))
    monkeypatch.setattr(runner, "_limiter", runner.RateLimiter(per_minute=1, per_day=10))
    with TestClient(app) as client:
        assert client.post("/api/agent/ask", json={"question": "  "}).status_code == 422
        assert client.post("/api/agent/ask", json={"question": "ok?", "lang": "fr"}).status_code == 422
        first = client.post("/api/agent/ask", json={"question": f"limit {uuid.uuid4().hex}"})
        created.append(_sse(first.text)[0][1]["run_id"])
        second = client.post("/api/agent/ask", json={"question": f"limit {uuid.uuid4().hex}"})
        assert second.status_code == 429 and int(second.headers["Retry-After"]) > 0
        assert second.json()["generated_at"] and "sources" in second.json()


def test_a_silent_stream_hits_our_deadline_and_falls_back(monkeypatch: Any) -> None:
    """Gemini once held streams open 90-145 s before a 503/504: after AGENT_IDLE_S of silence the
    gateway gives up on that model, cools it and streams from the next one."""
    monkeypatch.setenv("AGENT_IDLE_S", "0.3")
    monkeypatch.setenv("AGENT_CHAIN", "model-a,model-b")
    runner.reset_gateway_state()
    reply = types.GenerateContentResponse.model_validate(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]})  # fmt: skip

    class Stalls:
        def __init__(self) -> None:
            self.aio = self.models = self

        async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
            async def stream() -> Any:
                if model == "model-a":
                    await asyncio.sleep(30)
                yield reply

            return stream()

    async def go() -> tuple[str, list[Any]]:
        gateway = runner.Gateway("ask", "deadline-test", lambda: Stalls())
        contents = [types.Content(role="user", parts=[types.Part.from_text(text="hi")])]
        model, chunks, _ = await gateway.stream(contents, types.GenerateContentConfig(),
                                                prefer=None, priority=False)  # fmt: skip
        return model, [c async for c in chunks]

    def forget_calls() -> None:
        # Each call is counted against today's budget: left behind, a day of test runs used up the
        # fake models' budget and the gateway refused them ("daily budget used").
        with session_scope() as session:
            session.execute(text("DELETE FROM source_fetch WHERE source_key = 'gemini' "
                                 "AND meta->>'run_id' = 'deadline-test'"))  # fmt: skip

    forget_calls()
    try:
        model, chunks = asyncio.run(go())
        assert model == "model-b" and chunks[0].text == "ok"
        assert "504" in runner._cool_reason.get("model-a", "")  # cooled: new runs skip it
    finally:
        runner.reset_gateway_state()
        forget_calls()


def test_script_check_catches_any_other_script() -> None:
    """Live Punjabi bulletin, 2026-10-31: "ਗੁਣਵੱතා" (quality) carried two Sinhala letters."""
    from blindspot.agent.verifier import foreign_script

    assert foreign_script("ਹਵਾ ਦੀ ਗੁਣਵੱතා ਸੂਚਕ 92 [2]।", "pa") == ["ਗੁਣਵੱතා"]
    assert foreign_script("ਹਵਾ ਦੀ ਗੁਣਵੱਤਾ 92 µg/m³, 02:00 IST, CPCB, Sangrur [2]।", "pa") == []
    assert foreign_script("दिल्ली में ९२ µg/m³ ਪੰਜਾਬ [3]।", "hi") == ["ਪੰਜਾਬ"]


def test_a_bulletin_language_in_the_wrong_script_is_not_done(created: list[str]) -> None:
    from blindspot.agent import bulletin
    from blindspot.models.agent import AgentRun, Bulletin

    day = datetime(2001, 1, 2).date()  # a date no real bulletin has
    run_id = str(uuid.uuid4())
    good = {"verification": {"script_ok": True}}
    bad = {"verification": {"script_ok": False, "foreign_script_words": ["ਗੁਣਵੱතා"]}}
    created.append(run_id)
    with session_scope() as session:
        session.add(AgentRun(id=run_id, kind="bulletin", question="script test", lang="en",
                             started_at=utcnow()))  # fmt: skip
        session.flush()
        for lang, content in (("en", good), ("hi", good), ("pa", bad)):
            session.add(Bulletin(date=day, lang=lang, generated_at=utcnow(), model="m",
                                 content=content, run_id=run_id))  # fmt: skip
    assert bulletin._written(day) == {"en", "hi"}


def test_bulletin_latest_404_names_the_language() -> None:
    from blindspot.api.main import app

    with TestClient(app) as client:
        response = client.get("/api/bulletin/latest", params={"lang": "pa"})
        if response.status_code == 404:  # nothing written in the test database yet
            assert "Punjabi" in response.json()["detail"]
        else:
            assert response.json()["lang"] == "pa"
        assert client.get("/api/bulletin/latest", params={"lang": "xx"}).status_code == 422


def test_jobs_are_registered() -> None:
    from blindspot.jobs import JOBS

    assert JOBS["bulletin"].cron == "30 7-12 * * *"
    assert JOBS["featured_investigations"].after == ("bulletin",)


def test_old_answers_are_not_replayed(created: list[str]) -> None:
    """The cache window is 10 minutes: a stored run older than that is not replayed."""
    question = f"stale cache {uuid.uuid4().hex[:6]}"
    run = _run(question, ReplayClient(RECORDED["calls"]), created)
    _give_recorded_citations(run.id)
    assert runner.find_recent(runner.cache_key(question, "en"), 10)["id"] == run.id
    with session_scope() as session:
        session.execute(text("UPDATE agent_run SET started_at = :t WHERE id = :i"),
                        {"t": utcnow() - timedelta(minutes=11), "i": run.id})  # fmt: skip
    assert runner.find_recent(runner.cache_key(question, "en"), 10) is None


def test_featured_job_runs_once_a_day(monkeypatch: Any, created: list[str]) -> None:
    from blindspot.jobs import run_job

    monkeypatch.setattr(runner, "default_client_factory", lambda: ReplayClient(RECORDED["calls"]))
    first = run_job("featured_investigations", trigger="manual", chain=False)
    if first.ok and "runs" in (first.result or {}):
        created.extend(r["run_id"] for r in first.result["runs"])
        assert all(r["ok"] for r in first.result["runs"])
        assert len(first.result["runs"]) == 2
    else:  # this test database already holds today's featured runs
        assert first.ok and "skipped" in first.result
    again = run_job("featured_investigations", trigger="manual", chain=False)
    assert again.ok and "skipped" in again.result  # idempotent: no second set of Gemini calls
    with session_scope() as session:  # the two job_run rows this test wrote
        session.execute(text("DELETE FROM job_run WHERE name = 'featured_investigations' "
                             "AND trigger = 'manual' AND started_at >= :t"),
                        {"t": first.started_at})  # fmt: skip


def test_real_answer_with_numbers_verifies_against_what_the_model_saw() -> None:
    """A recorded real run (live CPCB/OpenAQ/IITM DSS data, 2026-10-30): every number in
    Gemini's answer is found in the tool outputs it was shown, and a changed number is not."""
    live = json.loads((FIXTURES / "gemini_run_live_air.json").read_text(encoding="utf-8"))
    answer = next(e for e in live["events"] if e["type"] == "answer")["text"]
    seen = [{"args": t["args"], "output": t.get("output")} for t in live["tools"]]
    evidence = build_evidence(seen, question=live["question"], now=datetime(2026, 10, 30, 18, 5,
                                                                            tzinfo=UTC))  # fmt: skip
    markers = {c["marker"] for c in next(e for e in live["events"]
                                         if e["type"] == "answer")["citations"]}  # fmt: skip
    report = verify(answer, evidence, known_markers=markers | {2, 5, 6})
    assert report.passed and report.checked >= 15 and report.verified == report.checked
    tampered = verify(answer.replace("201", "241"), evidence, known_markers=markers)
    assert not tampered.passed and any(u.startswith("241") for u in tampered.unverified)


SCARS = json.loads((FIXTURES / "endpoints_scars_risk.json").read_text(encoding="utf-8"))


def _recorded_scars_app() -> FastAPI:
    """Serves recorded real responses of our scars and risk endpoints."""
    app = FastAPI()
    case_path = f"/api/scars/{SCARS['case_id']}"

    @app.get("/api/scars/summary")
    def summary() -> JSONResponse:
        return JSONResponse(SCARS["/api/scars/summary"])

    @app.get("/api/scars")
    def scars() -> JSONResponse:
        return JSONResponse(SCARS["/api/scars"])

    @app.get(case_path)
    def case() -> JSONResponse:
        return JSONResponse(SCARS[case_path])

    @app.get("/api/risk")
    def risk() -> JSONResponse:
        return JSONResponse(SCARS["/api/risk"])

    return app


def test_scar_case_and_risk_tools_on_recorded_real_payloads() -> None:
    async def go() -> list[tools.ToolResult]:
        async with tools.InternalApi(app=_recorded_scars_app()) as api:
            return [await tools.run_tool(api, "get_burn_scars", {"limit": 5}),
                    await tools.run_tool(api, "get_case_file", {"scar_id": str(SCARS["case_id"])}),
                    await tools.run_tool(api, "get_burn_risk", {"limit": 5}),
                    await tools.run_tool(api, "get_case_file", {"scar_id": "drop table"})]  # fmt: skip

    scars, case, risk, bad = asyncio.run(go())
    season = SCARS["/api/scars/summary"]["season"]
    assert scars.ok and scars.data["season"]["clusters"] == season["clusters"]
    assert scars.data["season"]["unseen_share_pct"] == round(season["unseen_share"] * 100, 1)
    assert "candidates" in scars.data["method"]["status"]
    assert scars.data.get("candidate_unseen_audit") == tools.unseen_audit_block(SCARS["/api/scars/summary"])
    # the audit goes to the model as served (no rounding: the site says 95% CI 68%-96%)
    served = {"method": {"method": "random clusters", "audit": {"method": "random clusters, judged by eye",
              "candidate_unseen": {"audited": 23, "burnt": 20, "precision": 0.87, "precision_ci95": [0.679, 0.955]}}}}
    block = tools.unseen_audit_block(served)
    assert block is not None and block["precision_ci95"] == [0.679, 0.955] and block["audited"] == 23
    assert block["precision_pct"] == 87.0 and block["precision_ci95_pct"] == [67.9, 95.5]
    assert tools.unseen_audit_block({"method": {"audit": {}}}) is None and tools.unseen_audit_block(None) is None
    first = scars.data["clusters"][0]
    assert first["burnt_between"][0].endswith(" IST") and len(first["centroid_lon_lat"]) == 2
    assert case.ok and case.data["case"]["id"] == SCARS["case_id"]
    fires = SCARS[f"/api/scars/{SCARS['case_id']}"]["fires_nearby"]
    assert case.data["fires_nearby"]["explaining_the_scar"] == sum(f["explains_scar"] for f in fires)
    assert "geometry" not in json.dumps(case.data) and "series" not in case.data["weather_at_burn"]
    assert risk.ok and risk.data["totals"] == SCARS["/api/risk"]["totals"]
    assert not bad.ok and "must be a number" in (bad.error or "")


def test_real_e2e_answer_cites_the_right_sources() -> None:
    """The recorded real end-to-end run ("Why is Delhi's air bad today, and did it come from
    Punjab?", live data, 2026-10-30): every number cites a tool it came from; moving one
    citation to the wrong source is caught."""
    e2e = json.loads((FIXTURES / "gemini_run_e2e.json").read_text(encoding="utf-8"))
    answer = next(e for e in e2e["events"] if e["type"] == "answer")["text"]
    seen = [{"args": t["args"], "output": t.get("output")} for t in e2e["tools"]]
    now = datetime(2026, 10, 30, 18, 12, tzinfo=UTC)
    evidence = build_evidence(seen, question=e2e["question"], now=now)
    by_source = runner.attributed(e2e["tools"])
    markers = set().union(*(a.markers for a in by_source))
    good = verify(answer, evidence, markers, by_source)
    assert good.passed and good.checked >= 8, good.unverified
    wrong = answer.replace("30 Oct [23]", "30 Oct [1]")  # IITM DSS's share cited as FIRMS
    report = verify(wrong, evidence, markers, by_source)
    assert not report.passed
    assert any(u.startswith("1.75% cites [1] but comes from") for u in report.unverified)


ERROR_503 = json.loads((FIXTURES / "gemini_error_503.json").read_text(encoding="utf-8"))


def test_a_turn_cut_mid_stream_is_retried_on_another_model(created: list[str]) -> None:
    """Gemini sent 503 "high demand" after the first chunk (recorded 2026-10-30): nothing of the
    answer was shown, so the turn is redone on the next model and the run still answers."""
    overloaded = errors.ServerError(ERROR_503["code"], ERROR_503["details"])
    client = ReplayClient(RECORDED["calls"], break_first=[overloaded])
    run = _run(f"mid-stream test {uuid.uuid4().hex[:6]}", client, created)
    first, second = client.requests[0]["model"], client.requests[1]["model"]
    assert first != second and run.ok
    assert any(e["type"] == "run" and e.get("switched_from") == first for e in run.events)
    rows = _gemini_rows(run.id)
    assert rows[0]["http_status"] == 503 and not rows[0]["ok"]
    assert [e["id"] for e in run.events if e["type"] == "tool_call"] == _recorded_call_ids()


BULLETIN = json.loads((FIXTURES / "gemini_bulletin.json").read_text(encoding="utf-8"))


class BulletinReplay:
    """Replays the recorded real bulletin session per language, in the order it happened:
    English first met a 503, then streamed in full; Hindi and Punjabi each streamed part of a
    reply before a 503 cut it, then streamed in full on the next model."""

    def __init__(self) -> None:
        calls = BULLETIN["calls"]
        self.script = {"en": [calls[0], calls[1]], "hi": [calls[2], calls[4]],
                       "pa": [calls[3], calls[5]]}  # fmt: skip
        self.requests: list[tuple[str, str]] = []
        self.aio = self
        self.models = self

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
        turn = contents[0].parts[0].text
        lang = "hi" if "Translate into Hindi" in turn else "pa" if "Translate into Punjabi" in turn else "en"
        self.requests.append((lang, model))
        call = self.script[lang].pop(0)
        chunks = [types.GenerateContentResponse.model_validate_json(c) for c in call["chunks"]]
        broken = call.get("error")
        if broken and not chunks:
            raise errors.ServerError(ERROR_503["code"], ERROR_503["details"])

        async def stream() -> Any:
            for chunk in chunks:
                yield chunk
            if broken:
                # Live, both translations were already streaming when the 503s came; keep that
                # order here so the replay does not depend on how asyncio schedules the two.
                for _ in range(500):
                    if {"hi", "pa"} <= {lang for lang, _ in self.requests}:
                        break
                    await asyncio.sleep(0.01)
                raise errors.ServerError(ERROR_503["code"], ERROR_503["details"])

        return stream()

    async def aclose(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_bulletin_replay_checks_every_language(monkeypatch: Any, created: list[str]) -> None:
    """The recorded real bulletin (2026-10-30): facts from the live tools, English from Gemini,
    Hindi and Punjabi translated from it. Two Hindi mistranslations (PM10 110.0 as 1100.0, a
    smoke index 120.0 as 12.0) are caught against the English and their sentences removed."""
    from blindspot.agent import bulletin
    from blindspot.provenance import SourceRef

    async def recorded_facts(api: Any, book: Any, run: Any) -> tuple[dict[str, Any], int]:
        for marker, ref in sorted(BULLETIN["refs"].items(), key=lambda kv: int(kv[0])):
            assert book.mark(SourceRef.model_validate(ref)) == int(marker)
        facts = BULLETIN["facts"]
        return facts, sum(1 for v in facts.values() if "output" in v)

    monkeypatch.setattr(bulletin, "gather_facts", recorded_facts)
    client = BulletinReplay()
    now = datetime(2026, 10, 30, 18, 28, tzinfo=UTC)
    try:
        result = asyncio.run(bulletin.write_bulletin(now=now, force=True,
                                                     client_factory=lambda: client))  # fmt: skip
    finally:
        with session_scope() as session:
            created.extend(session.execute(
                text("SELECT DISTINCT run_id FROM bulletin WHERE date = :d"), {"d": now.date()}
            ).scalars())  # fmt: skip
    langs = result["languages"]
    assert set(langs) == {"en", "hi", "pa"} and all(v["verified"] for v in langs.values())
    assert langs["hi"]["removed_sentences"] == 2 and langs["pa"]["removed_sentences"] == 0
    assert langs["pa"]["script_ok"] and langs["hi"]["script_ok"]
    assert [lang for lang, _ in client.requests].count("hi") == 2  # retried after the 503
    from blindspot.api.main import app

    with TestClient(app) as http:
        hi = http.get("/api/bulletin/latest", params={"lang": "hi"}).json()
    assert hi["date"] == "2026-10-30" and hi["verification"]["passed"]
    removed = " ".join(hi["verification"]["removed_sentences"])
    assert "११००.०" in removed and "१२.०" in removed  # published with Devanagari digits
    assert hi["citations"] and all(c["source"]["key"] for c in hi["citations"])
    assert set(hi["available_langs"]) == {"en", "hi", "pa"}


# --- follow-up questions ------------------------------------------------------------------------
#
# The earlier run of a follow-up is the recorded real live-air investigation (2026-10-30), stored as
# if it had answered a few minutes ago. Its answer names Mundka at 201 µg/m³ [4].

LIVE = json.loads((FIXTURES / "gemini_run_live_air.json").read_text(encoding="utf-8"))
LIVE_ANSWER = next(e for e in LIVE["events"] if e["type"] == "answer")


def _stored_run(
    created: list[str],
    *,
    kind: str = "ask",
    ok: bool = True,
    age: timedelta = timedelta(minutes=5),
    question: str = LIVE["question"],
    answer: str = LIVE_ANSWER["text"],
    head: dict[str, Any] | None = None,
) -> str:
    """Stores an earlier run to follow up, started `age` ago; `head` adds fields to its `run`
    event (a stored follow-up's follow_up_of and context). Its verification has no honesty block,
    so it is never listed as an example."""
    from blindspot.models.agent import AgentRun

    run_id = str(uuid.uuid4())
    events = [dict(e) for e in LIVE["events"]]
    events[0] |= {"run_id": run_id, **(head or {})}
    started = utcnow() - age
    with session_scope() as session:
        session.add(AgentRun(
            id=run_id, kind=kind, question=question, lang="en", model=events[0]["model"],
            started_at=started, finished_at=started + timedelta(seconds=9), events=events,
            answer=answer if ok else None, citations=LIVE_ANSWER["citations"] if ok else [],
            verification={"passed": True} if ok else None, ok=ok,
            error=None if ok else "empty answer", tools=LIVE["tools"],
        ))  # fmt: skip
    created.append(run_id)
    return run_id


def _context_of(run_id: str, question: str = LIVE["question"], answer: str = LIVE_ANSWER["text"]) -> dict[str, str]:
    return {"run_id": run_id, "question": question, "answer": runner.context_answer(answer)}


def test_a_follow_up_gets_the_earlier_answer_as_context(created: list[str], monkeypatch: Any) -> None:
    """POST /api/agent/ask with follow_up_of: Gemini sees the earlier question and answer (without
    its citation markers or any tool output) as a first part marked as context, under the
    follow-up system prompt. The `run` event and the stored run name the thread, and a follow-up
    is never listed as an example."""
    from blindspot.agent import prompts
    from blindspot.api.main import app

    parent = _stored_run(created)
    gemini = ReplayClient(RECORDED["calls"])
    monkeypatch.setattr(runner, "default_client_factory", lambda: gemini)
    question = f"Which station was the worst? {uuid.uuid4().hex[:6]}"
    earlier = [_context_of(parent)]
    assert "[4]" not in earlier[0]["answer"] and "Mundka at 201 µg/m³ and" in earlier[0]["answer"]
    with TestClient(app) as http:
        response = http.post("/api/agent/ask", json={"question": question, "lang": "en",
                                                     "follow_up_of": parent.upper()})  # fmt: skip
        assert response.status_code == 200
        frames = _sse(response.text)
        run_id = frames[0][1]["run_id"]
        created.append(run_id)
        assert frames[0][1]["follow_up_of"] == parent and frames[0][1]["context"] == earlier
        assert frames[-1][0] == "done" and frames[-1][1]["ok"]
        context, asked = (part.text for part in gemini.requests[0]["contents"][0].parts)
        assert context == prompts.context_turn(earlier)
        assert context.startswith("EARLIER IN THIS CONVERSATION (context only, not evidence")
        assert LIVE["question"] in context and "Mundka at 201 µg/m³" in context
        assert "DPCC" not in context and "stations_reporting" not in context  # no tool output
        assert asked.startswith("Current time:") and asked.endswith(f"Question: {question}")
        assert gemini.requests[0]["config"].system_instruction == prompts.INVESTIGATOR_FOLLOW_UP_SYSTEM
        stored = http.get(f"/api/agent/runs/{run_id}").json()["run"]
        assert stored["follow_up_of"] == parent and stored["context"] == earlier
        assert stored["events"][0]["context"] == earlier  # the record of what the model was given
        first = http.get(f"/api/agent/runs/{parent}").json()["run"]
        assert first["follow_up_of"] is None and first["context"] == []
        started = datetime.fromisoformat(first["started_at"])
        until = datetime.fromisoformat(first["follow_up_until"])
        assert until - started == timedelta(hours=24)
        # Verified, honest and citing sources, yet not an example: "Which station was the
        # worst?" means nothing without its thread.
        assert stored["verification"]["passed"] and stored["verification"]["honesty"]["passed"]
        _give_recorded_citations(run_id)
        examples = http.get("/api/agent/examples?limit=20").json()["examples"]
        assert run_id not in [x["id"] for x in examples]


def test_a_follow_up_has_its_own_cache_key(created: list[str]) -> None:
    """The same words after another answer, or asked on their own, are another question: the key
    includes the run followed up, and only the same follow-up replays the stored answer."""
    question = f"And which station was the worst? {uuid.uuid4().hex[:6]}"
    parent, other = _stored_run(created), _stored_run(created)
    alone, after = runner.cache_key(question, "en"), runner.cache_key(question, "en", parent)
    assert alone == runner.cache_key(question, "en", None)  # questions on their own keep their key
    assert len({alone, after, runner.cache_key(question, "en", other)}) == 3
    run = asyncio.run(runner.run_to_end(question, "en", kind="ask", follow_up_of=parent,
                                        client_factory=lambda: ReplayClient(RECORDED["calls"])))  # fmt: skip
    created.append(run.id)
    _give_recorded_citations(run.id)  # the cache replays only answers that cite evidence
    with session_scope() as session:
        stored_key = session.execute(text("SELECT cache_key FROM agent_run WHERE id = :i"),
                                     {"i": run.id}).scalar_one()  # fmt: skip
    assert stored_key == after
    assert runner.find_recent(after, 10)["id"] == run.id
    assert runner.find_recent(alone, 10) is None
    again = asyncio.run(runner.ask(f"  {question.upper()} ", "en", "10.0.0.9", follow_up_of=parent))
    assert again.id == run.id and again.events[0]["cached"] is True
    assert again.events[0]["follow_up_of"] == parent


def test_a_follow_up_of_a_run_that_cannot_be_followed_is_refused(
    created: list[str], monkeypatch: Any
) -> None:
    """Unknown, without an answer, a bulletin, or over a day old: 400 with the reason, before the
    client is counted or Gemini is called. A follow-up such as "And in Ludhiana?" is not answered
    without its context."""
    from blindspot.api.main import app

    made: list[bool] = []

    def no_gemini() -> Any:
        made.append(True)
        raise runner.GeminiUnavailable("a refused follow-up must not reach Gemini", retryable=False)

    monkeypatch.setattr(runner, "default_client_factory", no_gemini)
    refused = {
        str(uuid.uuid4()): "no finished investigation has that id",
        _stored_run(created, ok=False): "ended without an answer",
        _stored_run(created, kind="bulletin"): "not an investigation",
        _stored_run(created, age=timedelta(hours=25)): "more than 24 h old",
    }
    with TestClient(app) as http:
        for parent, reason in refused.items():
            response = http.post("/api/agent/ask", json={"question": "And in Ludhiana?",
                                                         "follow_up_of": parent})  # fmt: skip
            body = response.json()
            assert response.status_code == 400 and reason in body["detail"], body
            assert body["status"] == 400 and body["sources"] == [] and body["generated_at"]
            assert response.headers["cache-control"] == "no-store"
        malformed = http.post("/api/agent/ask", json={"question": "And in Ludhiana?",
                                                      "follow_up_of": "not a run id"})  # fmt: skip
        assert malformed.status_code == 422
        # The earlier run cannot be read at all (database down): 503, try again, not a guess.
        from sqlalchemy.exc import OperationalError

        import blindspot.db

        def database_down() -> Any:
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))

        monkeypatch.setattr(blindspot.db, "SessionLocal", database_down)
        unreadable = http.post("/api/agent/ask", json={"question": "And in Ludhiana?",
                                                       "follow_up_of": next(iter(refused))})  # fmt: skip
        assert unreadable.status_code == 503 and "could not be read" in unreadable.json()["detail"]
        assert unreadable.headers["retry-after"] == "30"
    assert made == []  # Gemini was never asked
    assert not any(runner.limiter()._hits.values())  # and nothing was counted against the client


def _with_answer_text(call: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """A recorded Gemini call whose answer text has `old` replaced by `new`, once."""
    chunks, replaced = [], 0
    for raw in call["chunks"]:
        chunk = json.loads(raw)
        for candidate in chunk.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                if isinstance(part.get("text"), str) and not part.get("thought") and old in part["text"]:
                    part["text"] = part["text"].replace(old, new)
                    replaced += 1
        chunks.append(json.dumps(chunk))
    assert replaced == 1
    return {**call, "chunks": chunks}


def test_a_number_only_the_earlier_answer_gave_is_rejected(created: list[str], monkeypatch: Any) -> None:
    """The earlier answer is context, not evidence. Here the recorded final answer is changed to
    repeat the earlier "201 µg/m³" (Mundka) while every tool of this run is down, as it was when
    the run was recorded (an app with no routes): the number check rejects it, the revision pass
    says why, and the revised answer, without it, is the one kept."""
    from blindspot.agent import prompts

    parent = _stored_run(created)
    final = RECORDED["calls"][4]
    copied = _with_answer_text(final, "\nCaveat:", "\n- Mundka was the worst station at 201 µg/m³.\nCaveat:")
    gemini = ReplayClient([*RECORDED["calls"][:4], copied, final])  # the last one: the revision
    monkeypatch.setattr(runner, "InternalApi", lambda: tools.InternalApi(app=FastAPI()))
    run = asyncio.run(runner.run_to_end("Which station was the worst, and how bad was it?", "en",
                                        kind="ask", follow_up_of=parent,
                                        client_factory=lambda: gemini))  # fmt: skip
    created.append(run.id)
    assert "Mundka at 201 µg/m³" in gemini.requests[0]["contents"][0].parts[0].text  # shown...
    assert not any(t["ok"] for t in run.tools)  # ...and no tool of this run returned it
    first_pass = run.verification["first_pass_unverified"]
    assert any(item.startswith("201 µg/m³") for item in first_pass), first_pass
    revision = gemini.requests[5]["contents"][-1].parts[0].text
    assert "201 µg/m³" in revision and prompts.FOLLOW_UP_REVISION_NOTE in revision
    assert run.verification["revised"] and run.verification["passed"] and "201" not in run.answer


def test_a_thread_carries_its_last_two_questions_and_answers(created: list[str]) -> None:
    """A follow-up of a follow-up is given at most the last two earlier pairs
    (runner.MAX_CONTEXT_PAIRS), oldest first, each answer without its citation markers. A featured
    investigation can start a thread."""
    from blindspot.api.main import app

    e2e = json.loads((FIXTURES / "gemini_run_e2e.json").read_text(encoding="utf-8"))
    cited = next(e for e in e2e["events"] if e["type"] == "answer")["text"]  # a real cited answer
    first = _stored_run(created, kind="featured")
    after_first = runner.follow_up_context(first)
    assert after_first == [_context_of(first)]
    second = _stored_run(created, question="Did it come from Punjab?", answer=cited,
                         head={"follow_up_of": first, "context": after_first})  # fmt: skip
    after_second = runner.follow_up_context(second)
    assert after_second == [_context_of(first), _context_of(second, "Did it come from Punjab?", cited)]
    third = _stored_run(created, question="And Bawana?", answer=cited,
                        head={"follow_up_of": second, "context": after_second})  # fmt: skip
    after_third = runner.follow_up_context(third)
    assert [pair["run_id"] for pair in after_third] == [second, third]  # the first pair dropped off
    assert len(after_third) == runner.MAX_CONTEXT_PAIRS == 2
    assert "[" in cited and not any("[" in pair["answer"] for pair in after_third)
    with TestClient(app) as http:
        stored = http.get(f"/api/agent/runs/{third}").json()["run"]
    assert stored["follow_up_of"] == second
    assert [pair["run_id"] for pair in stored["context"]] == [first, second]
