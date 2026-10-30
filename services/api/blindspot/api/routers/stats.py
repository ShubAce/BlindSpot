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
from blindspot.api.routers.air import air_latest
from blindspot.api.routers.geostationary import diurnal
from blindspot.models.smoke import latest_back_summaries
from blindspot.provenance import SourceRef, utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats", tags=["stats"])

CACHE_S = 60  # the inputs change every 30 min at best; a minute keeps the page fast and honest


def _block(session: Session, name: str, build: Callable[[], Any]) -> tuple[dict[str, Any], list[SourceRef]]:
    """Run one block; on failure, roll back (so the next block's queries still work) and report."""
    try:
        return build()
    except Exception as exc:  # one broken block must not take the headline down
        log.exception("headline block %s failed", name)
        session.rollback()
        return {"available": False, "reason": f"could not compute: {type(exc).__name__}"}, []


def _air(session: Session) -> tuple[dict[str, Any], list[SourceRef]]:
    payload, refs = air_latest(session)
    block = headline.delhi_air(payload.get("receptors", []))
    block["smoke_trace"] = headline.delhi_smoke(latest_back_summaries(session))
    return block, refs


def _gk2a(session: Session) -> tuple[dict[str, Any], list[SourceRef]]:
    response = diurnal(session, days=3)  # the same numbers /api/geostationary/diurnal serves
    body = orjson.loads(response.body)
    refs = [SourceRef(**s) for s in body.get("sources", [])]
    return headline.gk2a_evening(body), refs


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
    for name, build in blocks.items():
        block, block_refs = _block(session, name, build)
        payload[name] = block
        for ref in block_refs:
            refs.setdefault((ref.key, ref.fetch_id), ref)
    return payload, list(refs.values())


@router.get("/headline")
def stats_headline(session: SessionDep) -> ORJSONResponse:
    """Fires in 24 h (India polygons), season FIRMS vs CREAMS, candidate unseen burns, Delhi
    PM2.5 now, last/next satellite look, and GK-2A evening activity, with sources."""
    payload, refs = get_or_build("headline", "headline", lambda: _build(session), ttl=CACHE_S)
    return respond(payload, refs, max_age=CACHE_S)
