"""GET /api/bulletin/latest?lang=en|hi|pa: the newest daily bulletin in one language.

The bulletin job (blindspot/jobs/bulletin.py) writes it every morning from tool outputs; this
route only reads the stored rows, so it is fast and works while Gemini is down.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from blindspot.agent.prompts import LANGUAGES
from blindspot.api import SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.models.agent import Bulletin
from blindspot.provenance import SourceRef, ref_for_fetch

router = APIRouter(prefix="/api/bulletin", tags=["bulletin"])


def _sources(row: Bulletin, session: SessionDep) -> list[SourceRef]:
    refs: list[SourceRef] = []
    seen: set[tuple[Any, ...]] = set()
    for citation in (row.content or {}).get("citations") or []:
        try:
            ref = SourceRef.model_validate(citation.get("source") or {})
        except ValueError:
            continue
        if (ref.key, ref.fetch_id, ref.url) not in seen:
            seen.add((ref.key, ref.fetch_id, ref.url))
            refs.append(ref)
    for fetch_id in (row.content or {}).get("gemini_fetch_ids") or []:
        ref = ref_for_fetch(session, fetch_id)
        if ref is not None and (ref.key, ref.fetch_id, ref.url) not in seen:
            seen.add((ref.key, ref.fetch_id, ref.url))
            refs.append(ref)
    return refs


@router.get("/latest")
@cached_route(ttl=60)
def latest(session: SessionDep, lang: Literal["en", "hi", "pa"] = "en"):  # noqa: ANN201
    """The newest bulletin in `lang`, with its citations and number-by-number verification."""
    row = session.execute(
        select(Bulletin)
        .where(Bulletin.lang == lang)
        .order_by(Bulletin.date.desc(), Bulletin.generated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No bulletin in {LANGUAGES[lang].split(',')[0]} has been written yet; "
            "the bulletin job writes one each morning at 07:30 IST.",
        )
    langs = session.execute(
        select(Bulletin.lang).where(Bulletin.date == row.date).order_by(Bulletin.lang)
    ).scalars().all()
    content = row.content or {}
    return respond(
        {
            "date": row.date.isoformat(),
            "lang": row.lang,
            "generated_at": row.generated_at,
            "model": row.model,
            "title": content.get("title"),
            "summary": content.get("summary"),
            "sections": content.get("sections") or [],
            "citations": content.get("citations") or [],
            "verification": content.get("verification"),
            "data_as_of": content.get("data_as_of"),
            "run_id": row.run_id,
            "available_langs": list(langs),
        },
        _sources(row, session),
        max_age=300,
    )
