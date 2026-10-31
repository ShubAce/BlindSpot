"""Tests of the honesty rule for the bulletin and the investigator (agent/verifier.py honesty
section, agent/bulletin.py, agent/runner.py answer checks, agent/prompts.py).

Gemini is never called. The texts are recorded real model output: the bulletin of 2026-10-31 as
Gemini wrote it in English, Hindi and Punjabi, with the tool outputs it was written from and the
burn-scar audit /api/scars/summary served that morning (tests/fixtures/agent/bulletin_2026-10-31.json).
That bulletin stated the candidate unseen share as fact with no audit precision, quoted "risk scores
of 100.0" and called FIRMS detections crop-residue fires: exactly what the check must catch.
Rows a test creates are deleted by run id.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import text

from blindspot.agent import bulletin, runner, verifier
from blindspot.agent.verifier import (
    build_evidence,
    check_honesty,
    foreign_script,
    honest_fallback,
    honesty_facts,
    verify,
)
from blindspot.db import init_db, session_scope
from blindspot.provenance import SourceRef

FX = json.loads((Path(__file__).parent / "fixtures" / "agent" / "bulletin_2026-10-31.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 30, 23, 28, 46, tzinfo=UTC)  # 04:58 IST on 31 Oct, when it was written


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


@pytest.fixture(autouse=True)
def _fresh_gateway() -> None:
    runner.reset_gateway_state()
    runner._limiter = None


@pytest.fixture
def created() -> Any:
    run_ids: list[str] = []
    yield run_ids
    with session_scope() as session:
        for run_id in run_ids:
            session.execute(text("DELETE FROM bulletin WHERE run_id = :id"), {"id": run_id})
            session.execute(text("DELETE FROM agent_run WHERE id = :id"), {"id": run_id})
            session.execute(text("DELETE FROM source_fetch WHERE source_key = 'gemini' AND meta->>'run_id' = :id"),
                            {"id": run_id})


def _facts(with_audit: bool = True) -> dict[str, Any]:
    facts = json.loads(json.dumps(FX["facts"]))
    if with_audit:
        audit = FX["scars_summary_audit"]
        facts["burn_scars_season"]["output"]["candidate_unseen_audit"] = {
            **{k: audit["candidate_unseen"][k] for k in ("precision", "precision_ci95", "audited")},
            "method": audit["method"]}
    return facts


def _units(lang: str) -> list[str]:
    reply = FX["replies"][lang]
    return [reply["title"], reply["summary"], *(s["body"] for s in reply["sections"])]


# --- the check -----------------------------------------------------------------------------------------


def test_the_live_bulletin_of_31_october_is_rejected_for_all_three_reasons() -> None:
    hf = honesty_facts(list(_facts(with_audit=False).values()))
    assert hf.season["unseen_area_ha"] == 86303.0 and hf.season["unseen_share_pct"] == 82.6 and hf.precision is None
    issues = check_honesty(_units("en"), "en", hf)
    kinds = {i.kind for i in issues}
    assert kinds == {"unseen_not_candidate", "raw_risk_score", "firms_called_crop_fires"}
    assert any("86303.0 ha" in i.sentence and "82.6%" in i.sentence for i in issues)
    assert any("100.0" in i.sentence for i in issues if i.kind == "raw_risk_score")
    # with the audit in the facts, the same text still fails: the sentence is not qualified
    hf = honesty_facts(list(_facts().values()))
    assert hf.precision == 0.87 and hf.precision_ci == (0.679, 0.955) and hf.audited == 23
    assert {i.kind for i in check_honesty(_units("en"), "en", hf)} == kinds


def test_a_qualified_sentence_passes_and_a_bare_one_does_not() -> None:
    hf = honesty_facts(list(_facts().values()))
    good = ("Candidate unseen burns cover 86303 ha, 82.6% of the mapped burn-scar area [28]. That share is an "
            "upper bound: in an audit of 23 candidates, 87% were real burns (95% CI 68%-96%) [28].")
    assert check_honesty([good], "en", hf) == []
    # "candidate" in the next sentence only (the live wording) is not enough
    late = "Unseen burns cover 86303 ha [28]. These are candidate unseen burns (95% CI 68%-96%, 87%)."
    assert [i.kind for i in check_honesty([late], "en", hf)] == ["unseen_not_candidate"]
    # "95% CI" names the interval: it must not count as its upper bound
    no_bounds = "Candidate unseen burns cover 82.6% of the burnt area [28], with an audit precision of 87% (95% CI)."
    assert [i.kind for i in check_honesty([no_bounds], "en", hf)] == ["unseen_without_precision"]
    # zero is not a claim; rounded numbers are
    assert check_honesty(["Unseen burns: 0.0 ha."], "en", hf) == []
    assert check_honesty(["About 83% of the scar area is unseen [28]."], "en", hf)[0].kind == "unseen_not_candidate"


@pytest.mark.parametrize("lang", ["hi", "pa"])
def test_translations_are_checked_by_their_numbers(lang: str) -> None:
    """The recorded Hindi and Punjabi texts carry the same unqualified share: caught without
    reading the words, by the numbers; the risk score is caught by the language's own word."""
    hf = honesty_facts(list(_facts().values()))
    kinds = {i.kind for i in check_honesty(_units(lang), lang, hf)}
    assert "unseen_not_candidate" in kinds or "unseen_without_precision" in kinds


@pytest.mark.parametrize("lang", ["en", "hi", "pa"])
def test_fallback_rewrites_from_the_tool_outputs_in_each_language(lang: str) -> None:
    facts = _facts()
    hf = honesty_facts(list(facts.values()))
    fixed, changed = honest_fallback(_units(lang), lang, hf)
    assert changed and check_honesty(fixed, lang, hf) == []
    assert foreign_script(" ".join(fixed), lang) == []
    sentence = verifier.unseen_sentence(lang, hf)
    assert sentence in " ".join(fixed)
    # every number of the rewritten sentences comes from the facts, cited to the block it came from
    for added in (sentence, verifier.risk_sentence(lang, hf)):
        assert verify(added, build_evidence([facts], now=NOW)).passed, added
    if lang == "en":
        text_ = " ".join(fixed)
        assert "100.0" not in text_ and "crop-residue fires" not in text_ and "232 fire detections" in text_
        assert "in an audit of 23 candidates, 87% were real burns (95% CI 68%-96%) [28]" in text_


def test_fallback_keeps_the_lines_of_an_answer() -> None:
    hf = honesty_facts(list(_facts().values()))
    answer = ("Burning continues in Punjab.\n- 232 crop-residue fires were detected in 24 hours [22].\n"
              "- Fields in Khem Karan show risk scores of 100.0 [31].\nCaveat: counts are a lower bound.")
    [fixed], changed = honest_fallback([answer], "en", hf)
    lines = fixed.split("\n")
    assert len(changed) == 2 and len(lines) == 4
    assert lines[1] == "- 232 fire detections were detected in 24 hours [22]."
    assert lines[2].startswith("- BlindSpot's burn-risk ranking") and "Chamar Tibbi, Moranwali, Model Town (Sangrur)" in lines[2]


# --- the bulletin: one retry, then the deterministic rewrite --------------------------------------------


def _chunk(text_: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text_)]), finish_reason="STOP")])


class RecordedBulletin:
    """Serves the recorded reply of each language, every time it is asked (as a model that ignores
    the retry would), and records what it was asked."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.aio = self
        self.models = self

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
        turn = contents[0].parts[0].text
        lang = "hi" if "Translate into Hindi" in turn else "pa" if "Translate into Punjabi" in turn else "en"
        self.requests.append((lang, model, turn))
        reply = {k: FX["replies"][lang][k] for k in ("title", "summary", "sections")}

        async def stream() -> Any:
            yield _chunk(json.dumps(reply, ensure_ascii=False))

        return stream()

    async def aclose(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_bulletin_retries_once_then_rewrites_and_stores_a_readable_row(monkeypatch: Any, created: list[str]) -> None:
    async def recorded_facts(api: Any, book: Any, run: Any) -> tuple[dict[str, Any], int]:
        for marker, ref in sorted(FX["refs"].items(), key=lambda kv: int(kv[0])):
            assert book.mark(SourceRef.model_validate(ref)) == int(marker)
        facts = _facts(with_audit=False)
        return facts, sum(1 for v in facts.values() if "output" in v)

    async def recorded_audit(api: Any, facts: dict[str, Any]) -> None:
        audit = _facts()["burn_scars_season"]["output"]["candidate_unseen_audit"]
        facts["burn_scars_season"]["output"]["candidate_unseen_audit"] = audit

    monkeypatch.setattr(bulletin, "gather_facts", recorded_facts)
    monkeypatch.setattr(bulletin, "add_unseen_audit", recorded_audit)
    client = RecordedBulletin()
    try:
        result = asyncio.run(bulletin.write_bulletin(now=NOW, force=True, client_factory=lambda: client))
    finally:
        with session_scope() as session:
            created.extend(session.execute(text("SELECT DISTINCT run_id FROM bulletin WHERE date = '2026-10-31'"))
                           .scalars())
    asked = [lang for lang, _, _ in client.requests]
    assert asked.count("en") == 2 and len(asked) <= 6 and result["gemini_calls"] == len(asked)
    retry = next(turn for lang, _, turn in client.requests[1:] if lang == "en")
    assert "An automatic check rejected these sentences" in retry and "100.0" in retry
    hf = honesty_facts(list(_facts().values()))
    with TestClient(__import__("blindspot.api.main", fromlist=["app"]).app) as http:
        for lang in ("en", "hi", "pa"):
            body = http.get("/api/bulletin/latest", params={"lang": lang}).json()
            assert body["date"] == "2026-10-31" and body["sections"]
            texts = [body["title"], body["summary"], *(s["body"] for s in body["sections"])]
            assert check_honesty(texts, lang, hf) == [], lang
            honesty = body["verification"]["honesty"]
            assert honesty["passed"] and honesty["first_pass_issues"]
            if lang == "en":
                assert honesty["retried"] and len(honesty["fallback_sentences"]) == 3
                assert "Candidate unseen burns" in " ".join(texts) and "(95% CI 68%-96%)" in " ".join(texts)
    # the stored row keeps the shape older readers expect: the same keys, honesty inside verification
    with session_scope() as session:
        content = session.execute(text("SELECT content FROM bulletin WHERE date = '2026-10-31' AND lang = 'pa'")).scalar()
    assert {"title", "summary", "sections", "citations", "verification", "data_as_of", "model"} <= content.keys()
    assert {"passed", "script_ok", "removed_sentences"} <= content["verification"].keys()


# --- the investigator ------------------------------------------------------------------------------------


class RecordedInvestigator:
    """A model that calls get_burn_scars and get_burn_risk, then answers with sentences of the recorded
    bulletin (the overclaims) and repeats them when asked to revise."""

    def __init__(self) -> None:
        self.calls = 0
        self.aio = self
        self.models = self
        en = FX["replies"]["en"]["sections"]
        self.answer = ("Burning goes on across Punjab.\n- " + en[1]["body"].split(". ")[0] + ".\n- "
                       + en[4]["body"].split(" These")[0] + "\n- " + en[3]["body"].split(". ")[1]
                       + "\nCaveat: satellite counts are a lower bound.")

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            parts = [types.Part(function_call=types.FunctionCall(id="c1", name="get_burn_scars", args={})),
                     types.Part(function_call=types.FunctionCall(id="c2", name="get_burn_risk", args={}))]
            chunk = types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(role="model", parts=parts), finish_reason="STOP")])
        else:
            chunk = _chunk(self.answer)

        async def stream() -> Any:
            yield chunk

        return stream()

    async def aclose(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_investigator_answer_gets_one_revision_then_the_rewrite(monkeypatch: Any, created: list[str]) -> None:
    from blindspot.agent.tools import ToolResult

    facts = _facts()
    refs = {int(m): SourceRef.model_validate(r) for m, r in FX["refs"].items()}

    async def recorded_tool(api: Any, name: str, args: dict[str, Any]) -> ToolResult:
        block = facts["burn_scars_season" if name == "get_burn_scars" else "burn_risk"]["output"]
        data = {k: v for k, v in block.items() if k != "sources"}
        markers = [int(s["marker"].strip("[]")) for s in block["sources"]]
        return ToolResult(True, data, [refs[m] for m in markers], name)

    monkeypatch.setattr(runner, "run_tool", recorded_tool)
    client = RecordedInvestigator()
    run = asyncio.run(runner.run_to_end("What did the satellites miss this season?", "en", kind="ask",
                                        client_factory=lambda: client))
    created.append(run.id)
    assert run.ok and client.calls == 3  # tools, answer, the one revision
    honesty = run.verification["honesty"]
    assert honesty["passed"] and len(honesty["first_pass_issues"]) == 3 and len(honesty["fallback_sentences"]) == 3
    hf = honesty_facts([{"args": t.get("args"), "output": t.get("output")} for t in run.tools])
    assert check_honesty([run.answer], "en", hf) == []
    assert "Candidate unseen burns" in run.answer and run.answer.count("\n- ") == 3
    answer_event = next(e for e in run.events if e["type"] == "answer")
    assert answer_event["text"] == run.answer
    stored = runner.replay({"id": run.id, "events": run.events, "started_at": run.started_at})  # a stored run replays as it was
    assert any(e["type"] == "verification" and e.get("honesty") for e in stored.events)


# --- repair: no false alarms, the AUC, more crop-fire phrasings, district figures kept ---------------------


def test_true_sentences_near_an_unseen_or_risk_number_are_left_alone() -> None:
    """Reviewer's finding: numbers that merely sit within 1.5% of a district's unseen share or area, or
    that equal a risk score but count fields or villages, were flagged and replaced by the unseen or
    risk paragraph. Built from the recorded facts: each district's share rounded, its total scar area."""
    facts = _facts()
    hf = honesty_facts(list(facts.values()))
    districts = facts["burn_scars_season"]["output"]["by_district"]
    benign = [f"Punjab accounted for {round(d['unseen_share_pct'])}% of the fire detections [22]." for d in districts]
    benign += [f"{d['district']} had {d['area_ha']} ha of burn scars this season [28]." for d in districts]
    benign += [f"Relative humidity in Delhi was {round(d['unseen_share_pct'])}% [7]." for d in districts]
    top = max(hf.risk_scores)
    benign += [f"Burn risk is ranked highest in Sangrur, where {round(top)} harvested fields are still unburnt [28].",
               f"The risk ranking covers {round(top)} villages [28].",
               "An audit found 87% of them were real burns (95% CI 67.9%-95.5%) [28]."]
    for sentence in benign:
        assert check_honesty([sentence], "en", hf) == [], sentence
    section = ("Candidate unseen burns cover 86303.0 ha, 82.6% of the mapped burn-scar area [28]. That share is an "
               "upper bound: an audit found 87% of them were real burns (95% CI 67.9%-95.5%) [28].")
    assert check_honesty([section], "en", hf) == [] and honest_fallback([section], "en", hf)[0] == [section]
    # the audit's interval written as one range ("67.9-95.5%") qualifies the share too
    ranged = "Candidate unseen burns cover about 86,000 ha [28]. Audit precision 87% (95% CI 67.9-95.5%) [28]."
    assert check_honesty([ranged], "en", hf) == []
    # still caught: an unseen figure without the word, in other units or rounded, and a quoted score
    assert check_honesty(["Satellites missed 863 km2 of burning [28]."], "en", hf)[0].kind == "unseen_not_candidate"
    assert check_honesty(["Fields in Khem Karan show risk scores of 100.0 [31]."], "en", hf)[0].kind == "raw_risk_score"


LOCAL_AUC_CLAIMS = {  # the local bulletin of 2026-10-31, as Gemini wrote it (stored with honesty "passed")
    "en": "Backtest validation of this village ranking achieved an AUC of 0.79 [28].",
    "hi": "इस गांव की रैंकिंग के बैकटेस्ट सत्यापन ने 0.79 का AUC हासिल किया [28]।",
    "pa": "ਇਸ ਪਿੰਡ ਦੀ ਰੈਂਕਿੰਗ ਦੀ ਬੈਕਟੈਸਟ ਵੈਲੀਡੇਸ਼ਨ ਨੇ 0.79 ਦਾ AUC ਪ੍ਰਾਪਤ ਕੀਤਾ [28]।",
}


@pytest.mark.parametrize("lang", ["en", "hi", "pa"])
def test_a_single_pair_auc_is_rejected_and_replaced_by_the_all_pairs_one(lang: str) -> None:
    facts = _facts()
    hf = honesty_facts(list(facts.values()))
    assert hf.auc_all["auc"] == 0.56 and hf.auc_all["pairs"] == 148 and 0.79 in hf.auc_other
    claim = LOCAL_AUC_CLAIMS[lang]
    assert [i.kind for i in check_honesty([claim], lang, hf)] == ["auc_not_all_pairs"]
    [fixed], changed = honest_fallback([claim], lang, hf)
    assert changed == [claim] and "0.79" not in fixed and "0.56" in fixed and "148" in fixed
    assert check_honesty([fixed], lang, hf) == [] and foreign_script(fixed, lang) == []
    assert verify(fixed, build_evidence([facts], now=NOW)).passed, fixed


def test_the_all_pairs_auc_needs_its_interval_when_there_is_one() -> None:
    hf = verifier.HonestyFacts(auc_all={"auc": 0.56, "ci": (0.491, 0.663), "pairs": 148, "markers": [28]},
                               auc_other=[0.701, 0.79, 0.5])
    assert [i.kind for i in check_honesty(["Across 148 pass pairs its AUC is 0.560 [28]."], "en", hf)] == \
        ["auc_without_interval"]
    good = "Across 148 pass pairs its AUC is 0.560 (95% CI 0.491-0.663; 0.5 is chance) [28]."
    assert check_honesty([good], "en", hf) == []
    assert check_honesty(["On the latest pair the AUC was 0.701 [28]."], "en", hf)[0].kind == "auc_not_all_pairs"
    sentence = verifier.auc_sentence("en", hf)
    assert "0.56 (95% CI 0.49-0.66; 0.5 is chance), so it is not clearly better than chance [28]" in sentence


def test_more_ways_of_calling_detections_crop_fires_are_caught() -> None:
    bare = verifier.HonestyFacts()
    for sentence in ("Satellites detected 232 crop fires in Punjab [22].", "There were 232 crop-burning fires [22].",
                     "There were 232 stubble-burning incidents [22].", "232 crop-residue burning cases [22]."):
        assert [i.kind for i in check_honesty([sentence], "en", bare)] == ["firms_called_crop_fires"], sentence
    facts = _facts()
    hf = honesty_facts(list(facts.values()))
    assert 232 in hf.firms_counts and 1043 not in hf.firms_counts  # CREAMS events are official residue fires
    assert check_honesty(["CREAMS recorded 1043 crop-residue burning events since 15 Sep [23]."], "en", hf) == []
    pa = next(s for s in FX["replies"]["pa"]["sections"][1]["body"].split("।") if "232" in s) + "।"
    assert [i.kind for i in check_honesty([pa], "pa", hf)] == ["firms_called_crop_fires"]
    [fixed], _ = honest_fallback([pa], "pa", hf)
    assert "232" in fixed and "ਪਛਾਣ" in fixed and "[22]" in fixed and foreign_script(fixed, "pa") == []
    assert check_honesty([fixed], "pa", hf) == [] and verify(fixed, build_evidence([facts], now=NOW)).passed


def test_a_district_figure_keeps_its_number_with_the_label_and_the_audit() -> None:
    """Reviewer's finding: the investigator's district bullets (live run 96afd348, local run d125f375)
    were dropped. Now a candidate-labelled one gains the audit, and an unlabelled one gains the label."""
    hf = honesty_facts(list(_facts().values()))
    district = next(d for d in _facts()["burn_scars_season"]["output"]["by_district"] if d["district"] == "Patiala")
    area = district["unseen_area_ha"]
    answer = (f"Most burning was unseen.\n- Patiala had {area} ha of candidate unseen burnt fields [28].\n"
              f"- In Patiala, {area} ha had no matching fire alert [28].\nCaveat: tillage can look like a burn.")
    kinds = [i.kind for i in check_honesty([answer], "en", hf)]
    assert kinds == ["unseen_without_precision", "unseen_not_candidate"]
    [fixed], changed = honest_fallback([answer], "en", hf)
    lines = fixed.split("\n")
    assert len(changed) == 2 and len(lines) == 4 and check_honesty([fixed], "en", hf) == []
    assert lines[1].startswith(f"- Patiala had {area} ha of candidate unseen burnt fields [28]. "
                               "In an audit of 23 candidate unseen burns, 87% were real burns (95% CI 67.9%-95.5%)")
    assert lines[2] == f"- In Patiala, {area} ha had no matching fire alert (candidate unseen burns) [28]."


def test_the_answer_check_adds_the_burn_scar_audit_to_the_scar_tool() -> None:
    """get_burn_scars leaves the audit out; the investigator's check fetches it (recorded reply of
    /api/scars/summary) into that tool's output, so a candidate share can be qualified, not dropped."""
    from blindspot.agent.tools import ApiReply

    output = dict(_facts(with_audit=False)["burn_scars_season"]["output"])
    run = runner.Run(id="test-audit", kind="ask", question="q", lang="en")
    run.tools = [{"id": "c1", "name": "get_burn_scars", "args": {}, "ok": True, "output": output}]
    audit = FX["scars_summary_audit"]
    asked: list[str] = []

    class Api:
        async def get(self, path: str, params: Any = None) -> ApiReply:
            asked.append(path)
            return ApiReply(path, 200, {"method": {"audit": {"candidate_unseen": audit["candidate_unseen"],
                                                             "method": audit["method"]}}})

    hf = asyncio.run(runner._with_unseen_audit(Api(), run))
    assert asked == ["/api/scars/summary"] and hf.precision == 0.87 and hf.audited == 23
    assert output["candidate_unseen_audit"]["precision_ci95"] == [0.679, 0.955]
    assert asyncio.run(runner._with_unseen_audit(Api(), run)).precision == 0.87 and len(asked) == 1


def test_a_stored_bulletin_is_rechecked_without_gemini(monkeypatch: Any) -> None:
    """The live bulletin of 2026-10-31 was written before the rule: recheck_stored() rewrites its
    overclaims in all three languages from the stored run's tool outputs (plus the recorded audit),
    re-verifies the numbers, keeps the row shape, and leaves a clean bulletin alone."""
    from datetime import date

    from blindspot.agent.verifier import cited_markers

    day = date(2099, 10, 31)  # a day no other test writes
    facts = _facts(with_audit=False)
    names = {label: name for label, name, _ in bulletin.FACT_PLAN}
    tools = [{"id": f"fact_{i + 1}", "name": names[label], "args": {}, "ok": True, **block}
             for i, (label, block) in enumerate(facts.items())]
    events = [{"type": "tool_result", "sources": [{**ref, "marker": int(m)} for m, ref in FX["refs"].items()]}]
    run_id = "test-recheck-0000-0000-000000000000"[:36]
    audit = _facts()["burn_scars_season"]["output"]["candidate_unseen_audit"]

    async def recorded_audit(api: Any) -> dict[str, Any]:
        return audit

    monkeypatch.setattr(bulletin, "unseen_audit", recorded_audit)
    from blindspot.models.agent import AgentRun, Bulletin

    with session_scope() as session:
        session.merge(AgentRun(id=run_id, kind="bulletin", question="test", lang="en", started_at=NOW, ok=True,
                               tools=tools, events=events))
        session.flush()
        for lang in ("en", "hi", "pa"):
            reply = {k: FX["replies"][lang][k] for k in ("title", "summary", "sections")}
            text_ = bulletin.full_text(reply)
            citations = [{"marker": m, "source": FX["refs"][str(m)]} for m in cited_markers(text_) if str(m) in FX["refs"]]
            session.add(Bulletin(date=day, lang=lang, model="recorded", run_id=run_id, generated_at=NOW,
                                 content={**reply, "citations": citations, "verification": {"passed": True},
                                          "data_as_of": "2026-10-31 04:58 IST", "model": "recorded"}))
    try:
        out = asyncio.run(bulletin.recheck_stored(day))
        assert out["written"] and out["audit_added"] and set(out["languages"]) == {"en", "hi", "pa"}
        hf = honesty_facts(list(_facts().values()))
        with session_scope() as session:
            rows = dict(session.execute(text("SELECT lang, content FROM bulletin WHERE date = :d"), {"d": day}).all())
            stored_tools = session.execute(text("SELECT tools FROM agent_run WHERE id = :id"), {"id": run_id}).scalar()
        for lang, content in rows.items():
            units = [content["title"], content["summary"], *(s["body"] for s in content["sections"])]
            assert check_honesty(units, lang, hf) == [], lang
            v = content["verification"]
            assert v["honesty"]["passed"] and v["honesty"]["fallback_sentences"] and v["script_ok"], lang
            assert {"title", "summary", "sections", "citations", "data_as_of", "model"} <= content.keys()
            assert content["data_as_of"] == "2026-10-31 04:58 IST" and content["model"] == "recorded"
        assert "Candidate unseen burns" in rows["en"]["sections"][4]["body"] and rows["en"]["verification"]["passed"]
        scars = next(t for t in stored_tools if t["name"] == "get_burn_scars")
        assert scars["output"]["candidate_unseen_audit"]["precision"] == 0.87
        again = asyncio.run(bulletin.recheck_stored(day))  # clean now: nothing to rewrite
        assert not again["written"] and again["unchanged"] == ["en", "hi", "pa"]
    finally:
        with session_scope() as session:
            session.execute(text("DELETE FROM bulletin WHERE date = :d"), {"d": day})
            session.execute(text("DELETE FROM agent_run WHERE id = :id"), {"id": run_id})
