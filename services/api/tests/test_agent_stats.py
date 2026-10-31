"""GET /api/agent/stats: the guardrail statistics, counted from agent_run rows written here.

The rows sit in March 1999 and the route's clock is frozen at noon IST on 10 March 1999, so the
window holds exactly these rows, whatever runs other tests (or the app) have stored; they are
deleted by id afterwards. Each row is one case the statistics must tell apart: an answer whose
figures all matched, one revised and fixed, one revised and still unmatched, one fixed by the
honesty fallback, a failed run, runs stored before the honesty check and before the revision
fields, and bulletins (a published one, a failed one, an early one, a translation-only one).
Every expected number below is worked out by hand from these rows.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.api.cache import HOURLY, cdn_rule
from blindspot.api.routers import agent_stats
from blindspot.db import init_db, session_scope
from blindspot.models.agent import AgentRun
from blindspot.provenance import utcnow

NOW = datetime(1999, 3, 10, 6, 30, tzinfo=UTC)  # 12:00 IST on 10 March 1999: no real run is that old


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


def _at(day: int, hour: int, minute: int = 0, month: int = 3) -> datetime:
    return datetime(1999, month, day, hour, minute, tzinfo=UTC)


def _tools(n: int) -> list[dict[str, Any]]:
    return [{"id": f"call_{i}", "name": "get_fire_activity", "args": {}, "ok": True} for i in range(n)]


def _honesty(issues: int = 0, fallback: int = 0, **extra: Any) -> dict[str, Any]:
    return {"passed": True, "first_pass_issues": [f"issue {i}" for i in range(issues)],
            "fallback_sentences": [f"sentence {i}" for i in range(fallback)], **extra}  # fmt: skip


def _report(checked: int, verified: int, **fields: Any) -> dict[str, Any]:
    """A verifier report as runner/bulletin store it: failing figures listed in `unverified`."""
    unverified = [f"figure {i}" for i in range(checked - verified)]
    return {"checked": checked, "verified": verified, "unverified": unverified, "passed": not unverified,
            "uncited": [], "bad_markers": [], **fields}  # fmt: skip


def _case(kind: str, ok: bool, lang: str, model: str | None, started_at: datetime, **columns: Any) -> dict[str, Any]:
    """The AgentRun columns of one case. A column left out is SQL NULL; None is JSON null, which
    is how save_run stores a failed run's verification."""
    return {"kind": kind, "ok": ok, "lang": lang, "model": model, "started_at": started_at, **columns}


CASES: dict[str, dict[str, Any]] = {
    # questions (kinds ask and featured)
    "q1_all_matched": _case("ask", True, "en", "model-a", _at(1, 3), duration_ms=8000, tools=_tools(3),
                            verification=_report(10, 10, revised=False, first_pass_unverified=[],
                                                 honesty=_honesty())),
    "q2_revised_fixed": _case("ask", True, "hi", "model-b", _at(3, 5), duration_ms=12000, tools=_tools(5),
                              verification=_report(8, 8, revised=True, first_pass_unverified=['72 ("72 µg/m³")'],
                                                   honesty=_honesty())),
    "q3_revised_still_unmatched": _case("featured", True, "en", "model-a", _at(3, 6), duration_ms=20000,
                                        tools=_tools(4),
                                        verification=_report(6, 5, revised=True,
                                                             first_pass_unverified=["9 (a)", "14 (b)"],
                                                             honesty=_honesty())),
    # revised for an honesty rule only (no unmatched figure), then two sentences rewritten by the
    # fallback; `tools` is JSON null, so its tool calls are counted from its events
    "q4_honesty_fixed": _case("ask", True, "pa", "model-a", _at(5, 10), duration_ms=9000, tools=None,
                              events=[{"type": "run"}, {"type": "tool_call"}, {"type": "tool_result"},
                                      {"type": "tool_call"}, {"type": "tool_result"}, {"type": "done"}],
                              verification=_report(4, 4, revised=True, first_pass_unverified=[],
                                                   honesty=_honesty(issues=1, fallback=2))),
    "q5_failed": _case("ask", False, "en", None, _at(7, 12), duration_ms=3000, tools=_tools(4), verification=None,
                       error="GeminiUnavailable: every model is cooling down"),
    # stored before the honesty check: no honesty block (and no figure in its answer)
    "q6_before_honesty": _case("ask", True, "en", "model-a", _at(8, 8), duration_ms=11000, tools=_tools(2),
                               verification=_report(0, 0, revised=False, first_pass_unverified=[])),
    # stored before the revision fields too, with no duration and no tools or events at all;
    # 18:45 UTC on 28 Feb is 00:15 IST on 1 March, so it is inside the window and on 1 March
    "q7_before_revision": _case("ask", True, "en", "model-b", _at(28, 18, 45, month=2),
                                verification={"checked": 5, "verified": 4, "unverified": ["3 (c)"],
                                              "passed": False}),
    # bulletins
    "b1_published": _case("bulletin", True, "en", "model-b", _at(2, 2), duration_ms=120000, tools=_tools(7),
                          verification=_report(30, 30, first_pass_unverified=["1,234 (d)"],
                                               removed_sentences=["The total was 1,234 [2]."], script_ok=True,
                                               foreign_script_words=[],
                                               honesty=_honesty(issues=1, fallback=1, retried=True,
                                                                after_retry_issues=["issue 0"]))),
    "b2_failed": _case("bulletin", False, "en", None, _at(4, 2), duration_ms=24000, tools=_tools(7),
                       error="503 UNAVAILABLE"),  # verification left SQL NULL
    # an early bulletin: the model rewrote it (revised), nothing removed, still one figure unmatched
    "b3_early": _case("bulletin", True, "en", "model-a", _at(6, 2), duration_ms=110000, tools=_tools(7),
                      verification=_report(15, 14, revised=True, first_pass_unverified=["w1", "w2"],
                                           removed_sentences=[])),
    "b4_translation_only": _case("bulletin", True, "en", "model-b", _at(6, 4), duration_ms=20000, tools=[],
                                 verification=_report(30, 30, first_pass_unverified=[], removed_sentences=[],
                                                      script_ok=True, foreign_script_words=[],
                                                      honesty=_honesty(retried=False, after_retry_issues=[]))),
    # outside the window: 23:30 IST on 28 Feb (before it), and half an hour after the frozen clock
    "x1_before_window": _case("ask", True, "en", "model-a", _at(28, 18, 0, month=2), duration_ms=5000,
                              tools=_tools(1), verification=_report(2, 2, revised=False, first_pass_unverified=[])),
    "x2_after_now": _case("bulletin", True, "en", "model-a", _at(10, 7), duration_ms=5000, tools=_tools(7),
                          verification=_report(3, 3, first_pass_unverified=[])),
}  # fmt: skip


@pytest.fixture
def stored_runs() -> Any:
    """The CASES as agent_run rows ({name: id}); afterwards exactly these rows are deleted."""
    ids = {name: str(uuid.uuid4()) for name in CASES}
    with session_scope() as session:
        for name, columns in CASES.items():
            duration = columns.get("duration_ms") or 0
            session.add(AgentRun(id=ids[name], question=f"stats test: {name}", cache_key=None,
                                 finished_at=columns["started_at"] + timedelta(milliseconds=duration),
                                 **columns))  # fmt: skip
    yield ids
    with session_scope() as session:
        session.execute(text("DELETE FROM agent_run WHERE id = ANY(:ids)"), {"ids": list(ids.values())})


def _series(**days: tuple[int, int, int, int, float | None]) -> list[dict[str, Any]]:
    """The 10 IST days of the window; `days` gives (runs, answered, all_matched, of, share) by day."""
    out = []
    for day in range(1, 11):
        runs, answered, matched, of, share = days.get(f"d{day}", (0, 0, 0, 0, None))
        out.append({"date": f"1999-03-{day:02d}", "runs": runs, "answered": answered, "all_matched": matched,
                    "of": of, "all_matched_share": share})  # fmt: skip
    return out


QUESTIONS = {
    "kinds": {"ask": 6, "featured": 1},
    "runs": 7, "answered": 6, "failed": 1,
    "first_run_at": "1999-02-28T18:45:00Z", "last_run_at": "1999-03-08T08:00:00Z",
    # 10/10 + 8/8 + 5/6 + 4/4 + 0/0 + 4/5 (the failed run has no report)
    "figures": {"checked": 33, "matched": 31, "matched_share": 0.9394, "runs": 6, "unknown": 0},
    "all_matched": {"runs": 4, "of": 6, "share": 0.6667, "without_figures": 1, "unknown": 0},
    # q2 and q3 were revised after unmatched figures; q2 then passed. q4 was revised for an honesty
    # rule only. q7 records no revision fields: unknown, not "not revised".
    "corrected_figures": {"runs": 2, "of": 5, "share": 0.4, "then_passed": 1, "then_passed_share": 0.5,
                          "sentences_removed": None, "unknown": 1, "how": agent_stats.GROUPS[0].correction},
    # q1-q4 record the honesty check; q6 and q7 are from before it
    "honesty": {"passed": 4, "of": 4, "share": 1.0, "first_draft_issues": 1, "first_draft_issues_share": 0.25,
                "then_passed": 1, "then_passed_share": 1.0, "fallback_runs": 1, "fallback_sentences": 2,
                "unknown": 2},
    # answered runs with a duration: 8000, 9000, 11000, 12000, 20000 -> p90 at rank 3.6
    "duration_ms": {"p50": 11000, "p90": 16800, "n": 5},
    # 3, 5, 4, 2 (from q4's events), 4, 2; q7 records none -> median of 2, 2, 3, 4, 4, 5
    "tool_calls": {"median": 3.5, "n": 6},
    "models": [{"model": "model-a", "runs": 4, "share": 0.6667}, {"model": "model-b", "runs": 2, "share": 0.3333}],
    "languages": [{"lang": "en", "runs": 5, "share": 0.7143}, {"lang": "hi", "runs": 1, "share": 0.1429},
                  {"lang": "pa", "runs": 1, "share": 0.1429}],
    "series": _series(d1=(2, 2, 1, 2, 0.5), d3=(2, 2, 1, 2, 0.5), d5=(1, 1, 1, 1, 1.0), d7=(1, 0, 0, 0, None),
                      d8=(1, 1, 1, 1, 1.0)),
}  # fmt: skip

BULLETINS = {
    "kinds": {"bulletin": 4},
    "runs": 4, "answered": 3, "failed": 1,
    "first_run_at": "1999-03-02T02:00:00Z", "last_run_at": "1999-03-06T04:00:00Z",
    "figures": {"checked": 75, "matched": 74, "matched_share": 0.9867, "runs": 3, "unknown": 0},
    "all_matched": {"runs": 2, "of": 3, "share": 0.6667, "without_figures": 0, "unknown": 0},
    # b1 (sentence removed, then passed) and b3 (rewritten by the model, still one figure unmatched)
    "corrected_figures": {"runs": 2, "of": 3, "share": 0.6667, "then_passed": 1, "then_passed_share": 0.5,
                          "sentences_removed": 1, "unknown": 0, "how": agent_stats.GROUPS[1].correction},
    "honesty": {"passed": 2, "of": 2, "share": 1.0, "first_draft_issues": 1, "first_draft_issues_share": 0.5,
                "then_passed": 1, "then_passed_share": 1.0, "fallback_runs": 1, "fallback_sentences": 1,
                "unknown": 1},
    # 20000, 110000, 120000 -> p90 at rank 1.8
    "duration_ms": {"p50": 110000, "p90": 118000, "n": 3},
    "tool_calls": {"median": 7.0, "n": 4},  # 7, 7, 7 and the translation-only run's 0
    "models": [{"model": "model-b", "runs": 2, "share": 0.6667}, {"model": "model-a", "runs": 1, "share": 0.3333}],
    "languages": [{"lang": "en", "runs": 4, "share": 1.0}],
    "series": _series(d2=(1, 1, 1, 1, 1.0), d4=(1, 0, 0, 0, None), d6=(2, 2, 1, 2, 0.5)),
}  # fmt: skip


def test_stats_count_every_case(stored_runs: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from blindspot.api.main import app

    # The cases are what they claim: a JSON-null report, an SQL-NULL report, JSON-null tools.
    with session_scope() as session:
        rows = session.execute(
            text("SELECT id, jsonb_typeof(verification), jsonb_typeof(tools), jsonb_typeof(events) "
                 "FROM agent_run WHERE id = ANY(:ids)"), {"ids": list(stored_runs.values())}).all()  # fmt: skip
    types = {run_id: (report, tools, events) for run_id, report, tools, events in rows}
    assert types[stored_runs["q5_failed"]][0] == "null" and types[stored_runs["b2_failed"]][0] is None
    assert types[stored_runs["q4_honesty_fixed"]][1:] == ("null", "array")
    assert types[stored_runs["q7_before_revision"]][1:] == (None, None)

    monkeypatch.setattr(agent_stats, "utcnow", lambda: NOW)
    with TestClient(app) as client:
        before = utcnow()
        response = client.get("/api/agent/stats", params={"days": 10})
        after = utcnow()
        assert response.status_code == 200, response.text
        body = response.json()
        again = client.get("/api/agent/stats", params={"days": 10}).json()

    assert body["days"] == 10
    assert body["window"] == {"from_utc": "1999-02-28T18:30:00Z", "to_utc": "1999-03-10T06:30:00Z",
                              "first_day_ist": "1999-03-01", "last_day_ist": "1999-03-10", "day_basis": "IST"}
    assert body["questions"] == QUESTIONS
    assert body["bulletins"] == BULLETINS
    assert "agent_run" in body["basis"] and "not from a model's opinion" in body["basis"]
    assert set(body["definitions"]) >= {"figures", "all_matched", "corrected_figures", "honesty", "of_and_unknown"}
    assert [s["key"] for s in body["sources"]] == ["gemini"]  # the agent's sources, as /api/agent/status cites
    assert body["generated_at"].endswith("Z")
    assert again == body  # the same answer from the in-process cache (same generated_at)

    # The CDN keeps it until the next HOURLY clock point (the catch-all /api/ rule).
    assert cdn_rule("/api/agent/stats").clock is HOURLY
    cache_control = response.headers["cache-control"]
    assert cache_control.startswith("public, max-age=") and "stale-while-revalidate=86400" in cache_control
    s_maxage = int(re.search(r"s-maxage=(\d+)", cache_control).group(1))
    bounds = (HOURLY.seconds_to_next(before), HOURLY.seconds_to_next(after))
    assert min(bounds) - 1 <= s_maxage <= max(bounds) + 1


def test_an_empty_window_says_so(stored_runs: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Today (IST) up to the frozen clock holds no run: the bulletin half an hour later is outside."""
    from blindspot.api.main import app

    monkeypatch.setattr(agent_stats, "utcnow", lambda: NOW)
    with TestClient(app) as client:
        body = client.get("/api/agent/stats", params={"days": 1}).json()
    assert body["window"]["first_day_ist"] == body["window"]["last_day_ist"] == "1999-03-10"
    for name, group in (("questions", body["questions"]), ("bulletins", body["bulletins"])):
        assert set(group["kinds"].values()) == {0}, name
        assert (group["runs"], group["answered"], group["failed"]) == (0, 0, 0)
        assert group["first_run_at"] is None and group["last_run_at"] is None
        assert group["figures"] == {"checked": 0, "matched": 0, "matched_share": None, "runs": 0, "unknown": 0}
        assert group["all_matched"]["share"] is None and group["corrected_figures"]["share"] is None
        assert group["honesty"]["share"] is None and group["honesty"]["then_passed_share"] is None
        assert group["duration_ms"] == {"p50": None, "p90": None, "n": 0}
        assert group["tool_calls"] == {"median": None, "n": 0}
        assert group["models"] == [] and group["languages"] == []
        assert group["series"] == [{"date": "1999-03-10", "runs": 0, "answered": 0, "all_matched": 0, "of": 0,
                                    "all_matched_share": None}]  # fmt: skip


def test_days_must_be_1_to_365() -> None:
    from blindspot.api.main import app

    with TestClient(app) as client:
        for days in (0, 366, -3):
            response = client.get("/api/agent/stats", params={"days": days})
            assert response.status_code == 422, days
            assert response.json()["status"] == 422 and "sources" in response.json()


def test_percentile_interpolates_as_postgres_does() -> None:
    samples = ([8000, 9000, 11000, 12000, 20000], [0, 7, 7, 7], [2, 2, 3, 4, 4, 5], [42], [3, 1, 2])
    with session_scope() as session:
        for values in samples:
            for q in (0.5, 0.9):
                expected = session.execute(
                    text("SELECT percentile_cont(CAST(:q AS float8)) WITHIN GROUP (ORDER BY x) "
                         "FROM unnest(CAST(:v AS float8[])) AS x"), {"q": q, "v": values}).scalar()  # fmt: skip
                assert agent_stats.percentile(values, q) == pytest.approx(expected), (values, q)
    assert agent_stats.percentile([], 0.5) is None
