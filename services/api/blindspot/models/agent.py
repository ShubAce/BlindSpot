"""Tables of the Gemini agent: every investigation run (agent_run) and the daily bulletin (bulletin).

agent_run keeps the whole story of a run so anyone can audit it later: the SSE events exactly as
they were streamed (which also lets an identical question within 10 minutes be replayed without
calling Gemini again), what each tool returned to the model, the final answer, its citations and
the number-by-number verification. Ids are UUID strings, so a run has an id before the database
is touched (the first SSE event carries it) and a run still streams if the database is down.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class AgentRun(Base):
    """One investigation (kind "ask") or one bulletin write-up (kind "bulletin")."""

    __tablename__ = "agent_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4 string
    kind: Mapped[str] = mapped_column(String(16))  # ask / bulletin / featured
    question: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(8))  # en / hi / pa
    model: Mapped[str | None] = mapped_column(String(64))  # the model that wrote the final answer
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    events: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)  # SSE events, in order
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)  # [{marker, source}]
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ok: Mapped[bool] = mapped_column(default=False)  # an answer was produced without an error
    error: Mapped[str | None] = mapped_column(Text)
    # sha256 of language + normalised question: finds a recent identical question to replay.
    cache_key: Mapped[str | None] = mapped_column(String(64))
    tools: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)  # what the model was shown
    usage: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)  # one entry per Gemini call

    __table_args__ = (
        Index("ix_agent_run_kind_started", "kind", "started_at"),
        Index("ix_agent_run_cache_started", "cache_key", "started_at"),
    )


class Bulletin(Base):
    """The daily public bulletin, one row per (date, language). Rewriting a day replaces its rows."""

    __tablename__ = "bulletin"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)  # the IST calendar day the bulletin is for
    lang: Mapped[str] = mapped_column(String(8))
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str | None] = mapped_column(String(64))
    # {title, summary, sections:[{heading, body}], citations, verification}
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_run.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (
        UniqueConstraint("date", "lang", name="uq_bulletin_date_lang"),
        Index("ix_bulletin_lang_date", "lang", "date"),
    )
