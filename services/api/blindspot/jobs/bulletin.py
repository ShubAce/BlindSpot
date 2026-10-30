"""Jobs of the agent module: the daily bulletin and the featured investigations.

bulletin                 07:30 IST: today's bulletin in English, Hindi and Punjabi. The crontab
                         also fires hourly until 12:30 IST, but a day already written is skipped
                         without calling Gemini, so those later runs only matter when 07:30 failed
                         (Gemini quota, no fresh data yet).
featured_investigations  after each bulletin run: the investigator answers the featured questions
                         once a day, so GET /api/agent/examples has fresh verified runs to show
                         even before anyone has asked. Skipped when today's runs exist.

Set BULLETIN_FORCE=1 to rewrite today's bulletin from the command line.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, time

from sqlalchemy import text

from blindspot.agent.bulletin import write_bulletin
from blindspot.agent.runner import cache_key, run_to_end
from blindspot.db import SessionLocal
from blindspot.geo import IST
from blindspot.jobs import job
from blindspot.provenance import utcnow

# The questions a visitor sees answered first. Product copy, not data: every number in the
# answers comes from the tools at the moment the job runs.
FEATURED_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Why is Delhi's air bad today, and did it come from Punjab?", "en"),
    ("Which districts burned the most in the last 24 hours, and what might the satellites have "
     "missed?", "en"),  # fmt: skip
)


@job("bulletin", cron="30 7-12 * * *")
def bulletin() -> dict:
    """Daily cited bulletin in English, Hindi and Punjabi (07:30 IST; hourly retries to 12:30)."""
    return asyncio.run(write_bulletin(force=os.environ.get("BULLETIN_FORCE") == "1"))


_DONE_TODAY = text(
    """
    SELECT cache_key FROM agent_run
    WHERE kind = 'featured' AND ok AND started_at >= :since AND cache_key = ANY(:keys)
    """
)


@job("featured_investigations", after="bulletin")
def featured_investigations() -> dict:
    """Investigator answers to the featured questions, once a day (for /api/agent/examples)."""
    midnight = datetime.combine(utcnow().astimezone(IST).date(), time(0), tzinfo=IST)
    keys = {cache_key(q, lang): (q, lang) for q, lang in FEATURED_QUESTIONS}
    with SessionLocal() as session:
        done = set(session.execute(_DONE_TODAY, {"since": midnight, "keys": list(keys)}).scalars())
    todo = [keys[k] for k in keys if k not in done]
    if not todo:
        return {"skipped": "today's featured investigations are already written"}

    async def run_all() -> list[dict]:
        out = []
        for question, lang in todo:  # one after the other: gentle on the per-minute quota
            run = await run_to_end(question, lang, kind="featured")
            out.append({"run_id": run.id, "ok": run.ok, "model": run.model,
                        "verified": (run.verification or {}).get("passed"), "error": run.error})  # fmt: skip
        return out

    results = asyncio.run(run_all())
    if not any(r["ok"] for r in results):
        raise RuntimeError(f"no featured investigation succeeded: {results}")
    return {"runs": results}
