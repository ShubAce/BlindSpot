"""GET /api/stats/headline: the front page's numbers in one call (architecture.md section 5).

Each block comes from blindspot.analytics.headline (or the same function another endpoint serves,
so the numbers match what the rest of the UI shows): fires in the last 24 h, the season against
CREAMS official counts, candidate unseen burns, Delhi's PM2.5 now, the last and next satellite
look at Punjab, and GK-2A's evening activity. A block that fails is replaced by
`{available: false, reason}` and the others still render: the landing page never goes blank
because one source is down. The whole answer is cached in memory for 60 s.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import orjson
from fastapi import APIRouter
from sqlalchemy.orm import Session

from blindspot.analytics import headline
from blindspot.api import SessionDep, respond
from blindspot.api.cache import get_or_build
from blindspot.api.responses import ORJSONResponse
from blindspot.api.routers.air import air_city
from blindspot.api.routers.geostationary import diurnal
from blindspot.api.routers.smoke import back_summaries
from blindspot.provenance import SourceRef, utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats", tags=["stats"])

CACHE_S = 60  # the inputs change every 30 min at best; a minute keeps the page fast and honest
PARTIAL_CACHE = "public, max-age=30, s-maxage=60"  # a headline with a failed block
OUTAGE_RETRY_S = 60


def _block(session: Session, name: str, build: Callable[[], Any]) -> tuple[dict[str, Any], list[SourceRef]]:
    """Run one block; on failure, roll back (so the next block's queries still work) and report."""
    try:
        return build()
    except Exception as exc:  # one broken block must not take the headline down
        log.exception("headline block %s failed", name)
        session.rollback()
        return {"available": False, "reason": f"could not compute: {type(exc).__name__}"}, []


def _air(session: Session) -> tuple[dict[str, Any], list[SourceRef]]:
    # Delhi's rows only (air_city, back_summaries): the same block air_latest and
    # latest_back_summaries gave, without reading every station and every city's whole summary.
    entry, refs = air_city(session, "Delhi")
    block = headline.delhi_air([entry] if entry is not None else [])
    block["smoke_trace"] = headline.delhi_smoke(back_summaries(session, "Delhi"))
    return block, refs


def _gk2a(session: Session) -> tuple[dict[str, Any], list[SourceRef]]:
    response = diurnal(session, days=3)  # the same numbers /api/geostationary/diurnal serves
    body = orjson.loads(response.body)
    refs = [SourceRef(**s) for s in body.get("sources", [])]
    block = headline.gk2a_evening(body)
    block["_newest"] = max((s["t"] for s in body.get("slots") or [] if s.get("t")), default=None)
    return block, refs


def _newest(name: str, block: dict[str, Any]) -> Any:
    """The time of the newest data behind a block's figure (see headline.FEEDS)."""
    if name == "fires_24h":
        return block.get("latest_acq_utc")
    if name == "delhi":
        return block.get("observed_at") or (block.get("last") or {}).get("observed_at")
    if name == "unseen_burns":
        return block.get("last_pass")
    if name == "gk2a_evening":
        return block.pop("_newest", None)
    return None


def _build(session: Session) -> tuple[dict[str, Any], list[SourceRef]]:
    now = utcnow()
    blocks = {
        "fires_24h": lambda: headline.fires_24h(session, now),
        "season": lambda: headline.season(session, now),
        "unseen_burns": lambda: headline.unseen_burns(session),
        "delhi": lambda: _air(session),
        "passes": lambda: headline.passes(session, now),
        "gk2a_evening": lambda: _gk2a(session),
    }
    payload: dict[str, Any] = {"as_of": now}
    refs: dict[tuple[str, int | None], SourceRef] = {}
    status, _ = _block(session, "freshness", lambda: (headline.feed_status(session), []))
    failed = status.get("available") is False and "reason" in status  # job_run unreadable
    every_block_broken = failed
    for name, build in blocks.items():
        block, block_refs = _block(session, name, build)
        broken = str(block.get("reason", "")).startswith("could not compute")
        failed = failed or broken
        every_block_broken = every_block_broken and broken
        newest = _newest(name, block)
        if name in headline.FEEDS and not broken and not (status.get("available") is False):
            block["freshness"] = headline.freshness(name, status, now, newest)
        payload[name] = block
        for ref in block_refs:
            refs.setdefault((ref.key, ref.fetch_id), ref)
    if failed:
        payload["_failed"] = "all" if every_block_broken else "some"
    return payload, list(refs.values())


@router.get("/headline")
def stats_headline(session: SessionDep) -> ORJSONResponse:
    """Fires in 24 h (India polygons), season FIRMS vs CREAMS, candidate unseen burns, Delhi
    PM2.5 now, last/next satellite look, and GK-2A evening activity, with sources. Each block
    carries `freshness` (headline.FEEDS): its feeding job's last success and a stale flag."""
    payload, refs = get_or_build("headline", "headline", lambda: _build(session), ttl=CACHE_S)
    failed = payload.get("_failed")
    if failed:
        body = {k: v for k, v in payload.items() if k != "_failed"}
        if failed == "all":
            # Nothing could be computed: the database is down or suspended. A 503 (same body) lets
            # the CDN keep answering with the last good headline (stale-if-error covers 5xx only),
            # and nobody caches the error itself.
            return respond(body, refs, status_code=503,
                           headers={"Cache-Control": "no-store", "Retry-After": str(OUTAGE_RETRY_S)})
        # Some blocks failed: shared caches keep this for a minute only (the CDN would otherwise
        # keep it until the next clock point), so a failing block is retried soon without every
        # visitor's poll reaching the database.
        return respond(body, refs, headers={"Cache-Control": PARTIAL_CACHE})
    return respond(payload, refs, max_age=CACHE_S)
