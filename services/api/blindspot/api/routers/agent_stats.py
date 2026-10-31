"""GET /api/agent/stats?days=30: how the investigator's guardrails did, counted from every stored run.

The investigator and the daily bulletin check their text before it becomes the final answer: every
figure is matched against the tool outputs the model was shown (agent/verifier.py), a draft with a
figure that matches nothing is corrected, and the honesty rules (candidate unseen burns with the
audit precision, no raw risk scores, ...) are enforced, by the model's own rewrite first and by a
deterministic rewrite from the tool outputs last. Each run stores that report in
agent_run.verification. This route counts, over the last `days` IST days:

- runs, answered, failed;
- figures checked, and the share that matched;
- runs whose every figure matched (the verifier's `passed`);
- runs corrected after the first draft had unmatched figures, and how many then passed;
- runs whose first draft broke an honesty rule, and how many final texts then passed;
- sentences rewritten by the deterministic honesty fallback;
- median and 90th-percentile duration, median tool calls per run;
- the models that wrote the final texts, the languages, the first and last run;
- a per-IST-day series of runs and of the share whose every figure matched.

Questions (kinds ask and featured) and bulletins are counted separately, because they correct
unmatched figures differently: the investigator asks the model to rewrite its answer, the bulletin
removes the sentences that hold them.

Older rows lack fields added later (the honesty check exists since 31 Oct 2026). A missing field is
unknown, never false: such a run is left out of that block's shares and counted as `unknown`.

Neon egress: Postgres reduces each run to one small row of flags and counts (about 90 bytes of
values; never the answer, the events or the tool outputs, which run to 5-25 KB a run). The CDN
keeps the answer until the next HOURLY clock point (api/cache.py: the catch-all /api/ rule), so a
new run shows from the next point on; in process, answers are kept for CACHE_S.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import text
from starlette.responses import Response

from blindspot.api import SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.geo import IST
from blindspot.provenance import latest_refs, utcnow

router = APIRouter(prefix="/api/agent", tags=["agent"])

CACHE_S = 300  # in process; the CDN keeps it to the next HOURLY point, browsers at most 5 min

Row = Mapping[str, Any]


@dataclass(frozen=True)
class Group:
    name: str
    kinds: tuple[str, ...]
    removes_sentences: bool  # bulletins remove sentences; the investigator asks for a rewrite
    correction: str  # how a draft with unmatched figures is corrected, in words


GROUPS = (
    Group("questions", ("ask", "featured"), False,
          "The model is sent the figures that matched nothing and rewrites its answer once; the rewrite "
          "is kept unless it has more problems than the draft (verification.revised with "
          "first_pass_unverified)."),
    Group("bulletins", ("bulletin",), True,
          "The sentences that hold a figure matching nothing are removed before the bulletin is "
          "published (verification.first_pass_unverified and removed_sentences; the earliest "
          "bulletin runs asked the model to rewrite instead, verification.revised)."),
)  # fmt: skip

BASIS = (
    "Counted from BlindSpot's own stored runs (the agent_run table), not from a model's opinion of "
    "itself: every investigation and every daily bulletin is stored with the verifier's report on "
    "its final text (each figure checked against the tool outputs the model was shown) and, since "
    "31 Oct 2026, with the honesty check. A replay of a stored answer (the same question again "
    "within 10 minutes) is not a new run. The source listed is the newest recorded Gemini call; "
    "Gemini wrote the texts, BlindSpot's checks produced these counts."
)

DEFINITIONS = {
    "questions": "Investigator runs: kind ask (a visitor's question) and featured (the daily job's own "
                 "questions, kinds.featured).",
    "bulletins": "Daily bulletin runs. A run writes the English bulletin and translates it; its stored "
                 "report is the English text's (a run that only translated a stored English bulletin "
                 "keeps its first translation's), and it is stored as English.",
    "answered": "Runs stored with ok = true: a text was produced without an error. failed = runs - "
                "answered: Gemini unavailable, no answer, or a bulletin language that could not be written.",
    "figures": "Every number, time and date in each answered run's final text, as the verifier counted "
               "them. Matched: found, within rounding, in the evidence the run was checked against (its "
               "tools' outputs, plus the question, the clock and the tools' descriptions; a translation "
               "against its English) and, when cited, coming from the tool its marker names.",
    "all_matched": "Answered runs whose final text passed the number check: every figure matched and every "
                   "citation marker named a real source. without_figures: of these, the runs with no "
                   "figure to check.",
    "corrected_figures": "Runs whose first draft had a figure that matched nothing and was corrected (see "
                         "how), and how many of them then passed the number check.",
    "honesty": "The honesty rules: candidate unseen burns named as candidates and given with the audit "
               "precision and its 95% interval; no raw burn-risk scores; the ranking's AUC only over all "
               "pass pairs; FIRMS detections not called crop fires. first_draft_issues: runs whose first "
               "draft broke a rule; then_passed: of those, the final texts that passed; "
               "fallback_sentences: sentences rewritten from the tool outputs by the deterministic "
               "fallback when a rule was still broken after the model's own rewrite.",
    "of_and_unknown": "In each block, of = the answered runs that record the field, and unknown = the "
                      "answered runs stored before it existed. Unknown runs are left out of the shares, "
                      "never counted as false.",
    "duration_ms": "How long each answered run took, start to end: the median (p50) and the 90th "
                   "percentile, interpolated between ranks as Postgres percentile_cont does.",
    "tool_calls": "Tool calls a run made (its stored tool outputs, or its tool_call events where those are "
                  "missing): the median over the runs that record them.",
    "models": "The Gemini model that wrote each answered run's final text.",
    "languages": "The language stored on each run.",
    "series": "Per IST day of the window: runs, answered runs, and how many of those that record a number "
              "check passed it (all_matched out of of).",
}  # fmt: skip

# One row per run: its verification report reduced to flags and counts. Every field is read only
# when it has the expected JSON type, so a field an older row does not have comes back NULL
# (unknown), never false. A failed run stores verification as JSON null: all NULL as well.
_RUNS_SQL = text(
    """
    WITH run AS (
        SELECT kind, ok, lang, model, started_at, duration_ms, tools, events,
               verification AS v, verification->'honesty' AS h
        FROM agent_run
        WHERE kind IN ('ask', 'featured', 'bulletin')
          AND started_at >= :since AND started_at <= :until
    )
    SELECT kind, ok, lang, model, started_at, duration_ms,
           CASE WHEN jsonb_typeof(v->'checked') = 'number'
                THEN round((v->>'checked')::numeric)::int END                   AS checked,
           CASE WHEN jsonb_typeof(v->'verified') = 'number'
                THEN round((v->>'verified')::numeric)::int END                  AS matched,
           CASE WHEN jsonb_typeof(v->'passed') = 'boolean'
                THEN (v->>'passed')::boolean END                                AS passed,
           CASE WHEN jsonb_typeof(v->'revised') = 'boolean'
                THEN (v->>'revised')::boolean END                               AS revised,
           CASE WHEN jsonb_typeof(v->'first_pass_unverified') = 'array'
                THEN jsonb_array_length(v->'first_pass_unverified') END         AS first_unmatched,
           CASE WHEN jsonb_typeof(v->'removed_sentences') = 'array'
                THEN jsonb_array_length(v->'removed_sentences') END             AS removed,
           CASE WHEN jsonb_typeof(h->'passed') = 'boolean'
                THEN (h->>'passed')::boolean END                                AS honesty_passed,
           CASE WHEN jsonb_typeof(h->'first_pass_issues') = 'array'
                THEN jsonb_array_length(h->'first_pass_issues') END             AS honesty_first_issues,
           CASE WHEN jsonb_typeof(h->'fallback_sentences') = 'array'
                THEN jsonb_array_length(h->'fallback_sentences') END            AS fallback_sentences,
           -- one entry per tool call; the events stream has the same calls when tools is missing
           CASE WHEN jsonb_typeof(tools) = 'array' THEN jsonb_array_length(tools)
                WHEN jsonb_typeof(events) = 'array'
                THEN (SELECT count(*)::int FROM jsonb_array_elements(events) e
                      WHERE e->>'type' = 'tool_call') END                       AS tool_calls
    FROM run
    ORDER BY started_at
    """
)


# --- counting ------------------------------------------------------------------------------------


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-quantile, interpolating linearly between ranks (what Postgres percentile_cont gives)."""
    if not values:
        return None
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _recorded(rows: Iterable[Row], *fields: str) -> list[Row]:
    """The rows that record every one of `fields`; the others are unknown for it, not false."""
    return [r for r in rows if all(r[f] is not None for f in fields)]


def _distribution(values: Iterable[Any], key: str) -> list[dict[str, Any]]:
    """[{key: value, runs, share}], most frequent first (ties by name)."""
    counts = Counter(values)
    total = sum(counts.values())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    return [{key: value, "runs": n, "share": _share(n, total)} for value, n in ranked]


def _figures(answered: list[Row]) -> dict[str, Any]:
    rec = _recorded(answered, "checked", "matched")
    checked = sum(r["checked"] for r in rec)
    matched = sum(r["matched"] for r in rec)
    return {"checked": checked, "matched": matched, "matched_share": _share(matched, checked),
            "runs": len(rec), "unknown": len(answered) - len(rec)}  # fmt: skip


def _all_matched(answered: list[Row]) -> dict[str, Any]:
    rec = _recorded(answered, "passed")
    passed = [r for r in rec if r["passed"]]
    return {"runs": len(passed), "of": len(rec), "share": _share(len(passed), len(rec)),
            "without_figures": sum(1 for r in passed if r["checked"] == 0),
            "unknown": len(answered) - len(rec)}  # fmt: skip


def _corrected(group: Group, answered: list[Row]) -> dict[str, Any]:
    """Drafts with a figure that matched nothing, corrected, and whether the final text then passed.

    The investigator stores the first draft's unmatched figures only when it kept the model's
    rewrite (revised), so its count is "revised with first_pass_unverified"; a bulletin always
    stores them and removes the sentences that hold them."""
    if group.removes_sentences:
        rec = _recorded(answered, "first_unmatched")
        fixed = [r for r in rec if r["first_unmatched"] > 0]
        removed = sum(r["removed"] for r in rec if r["removed"] is not None)
    else:
        rec = _recorded(answered, "revised", "first_unmatched")
        fixed = [r for r in rec if r["revised"] and r["first_unmatched"] > 0]
        removed = None  # the investigator never removes sentences
    then = [r for r in fixed if r["passed"]]
    return {"runs": len(fixed), "of": len(rec), "share": _share(len(fixed), len(rec)),
            "then_passed": len(then), "then_passed_share": _share(len(then), len(fixed)),
            "sentences_removed": removed, "unknown": len(answered) - len(rec),
            "how": group.correction}  # fmt: skip


def _honesty(answered: list[Row]) -> dict[str, Any]:
    rec = _recorded(answered, "honesty_passed", "honesty_first_issues", "fallback_sentences")
    passed = [r for r in rec if r["honesty_passed"]]
    broke = [r for r in rec if r["honesty_first_issues"] > 0]
    then = [r for r in broke if r["honesty_passed"]]
    return {"passed": len(passed), "of": len(rec), "share": _share(len(passed), len(rec)),
            "first_draft_issues": len(broke), "first_draft_issues_share": _share(len(broke), len(rec)),
            "then_passed": len(then), "then_passed_share": _share(len(then), len(broke)),
            "fallback_runs": sum(1 for r in rec if r["fallback_sentences"] > 0),
            "fallback_sentences": sum(r["fallback_sentences"] for r in rec),
            "unknown": len(answered) - len(rec)}  # fmt: skip


def _durations(answered: list[Row]) -> dict[str, Any]:
    values = [r["duration_ms"] for r in answered if r["duration_ms"] is not None]
    p50, p90 = percentile(values, 0.5), percentile(values, 0.9)
    return {"p50": None if p50 is None else round(p50), "p90": None if p90 is None else round(p90),
            "n": len(values)}  # fmt: skip


def _tool_calls(runs: list[Row]) -> dict[str, Any]:
    values = [r["tool_calls"] for r in runs if r["tool_calls"] is not None]
    median = percentile(values, 0.5)
    return {"median": None if median is None else round(median, 1), "n": len(values)}


def _ist_day(at: datetime) -> date:
    return at.astimezone(IST).date()


def _series(runs: list[Row], first_day: date, last_day: date) -> list[dict[str, Any]]:
    """One entry per IST day of the window, days without runs included (zeros, share null)."""
    by_day: dict[date, list[Row]] = {}
    for r in runs:
        by_day.setdefault(_ist_day(r["started_at"]), []).append(r)
    out = []
    day = first_day
    while day <= last_day:
        rows = by_day.get(day, [])
        answered = [r for r in rows if r["ok"]]
        judged = _recorded(answered, "passed")
        matched = sum(1 for r in judged if r["passed"])
        out.append({"date": day.isoformat(), "runs": len(rows), "answered": len(answered),
                    "all_matched": matched, "of": len(judged),
                    "all_matched_share": _share(matched, len(judged))})  # fmt: skip
        day += timedelta(days=1)
    return out


def summarise(group: Group, runs: list[Row], first_day: date, last_day: date) -> dict[str, Any]:
    """Every statistic of one group, from its runs' rows (as _RUNS_SQL returns them)."""
    answered = [r for r in runs if r["ok"]]
    starts = [r["started_at"] for r in runs]
    return {
        "kinds": {kind: sum(1 for r in runs if r["kind"] == kind) for kind in group.kinds},
        "runs": len(runs),
        "answered": len(answered),
        "failed": len(runs) - len(answered),
        "first_run_at": min(starts).astimezone(UTC) if starts else None,
        "last_run_at": max(starts).astimezone(UTC) if starts else None,
        "figures": _figures(answered),
        "all_matched": _all_matched(answered),
        "corrected_figures": _corrected(group, answered),
        "honesty": _honesty(answered),
        "duration_ms": _durations(answered),
        "tool_calls": _tool_calls(runs),
        "models": _distribution((r["model"] for r in answered), "model"),
        "languages": _distribution((r["lang"] for r in runs), "lang"),
        "series": _series(runs, first_day, last_day),
    }


# --- route ---------------------------------------------------------------------------------------


@router.get("/stats")
@cached_route(ttl=CACHE_S)
def guardrail_stats(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365, description="IST days to cover, today included")] = 30,
) -> Response:
    """How often the investigator's and the bulletin's figures matched their evidence, how often a
    draft was corrected (unmatched figures, honesty rules) and whether the correction passed, with
    durations, tool calls, models and languages, and a per-IST-day series. Counted from every
    stored run of the window (`basis`); fields older runs lack are `unknown`, never false."""
    now = utcnow()
    last_day = _ist_day(now)
    first_day = last_day - timedelta(days=days - 1)
    since = datetime.combine(first_day, time(0), tzinfo=IST).astimezone(UTC)
    rows = session.execute(_RUNS_SQL, {"since": since, "until": now}).mappings().all()
    window = {"from_utc": since, "to_utc": now.astimezone(UTC), "first_day_ist": first_day.isoformat(),
              "last_day_ist": last_day.isoformat(), "day_basis": "IST"}  # fmt: skip
    payload: dict[str, Any] = {"days": days, "window": window, "basis": BASIS}
    for group in GROUPS:
        runs = [r for r in rows if r["kind"] in group.kinds]
        payload[group.name] = summarise(group, runs, first_day, last_day)
    payload["definitions"] = DEFINITIONS
    return respond(payload, latest_refs(session, ["gemini"]), max_age=CACHE_S)
