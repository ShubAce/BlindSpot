"""smoke_run: one row per particle-model run (architecture.md section 4).

- kind 'forward': smoke from recent fires and candidate unseen burn scars, `receptors` holds each
  city's smoke-index series, `trails` the browser trails (both ensemble members).
- kind 'back': one row per city: where the city's air came from over the last 72 h; `summary`
  holds the contract summary plus the verdict, cone and per-member numbers; `trails` the trails.
- kind 'validation': the modelled smoke index against observed PM2.5 (air_obs) for the cities
  that have ground stations; `summary` holds the correlations, `receptors` the hourly series.

`trails` is the big column (MBs). The job keeps it only on the newest runs and clears it on older
ones; summaries and receptor series stay, so verdicts and series can be audited later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from blindspot.db import Base


class SmokeRun(Base):
    __tablename__ = "smoke_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # forward / back / validation
    city: Mapped[str | None] = mapped_column(String(64))  # back runs only
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # "now" for the run
    t0: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # start of the integration
    hours: Mapped[float]  # length of the integration (back runs: hours back from run_at)
    met_run_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))  # winds used
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))  # method, inputs, sources, timing
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    trails: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    receptors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_smoke_run_kind_run_at", "kind", "run_at"),
        Index("ix_smoke_run_kind_city_run_at", "kind", "city", "run_at"),
    )


def latest_id(session: Session, kind: str, city: str | None = None) -> int | None:
    """Id of the newest run of `kind` (for back runs, of that city, case-insensitive)."""
    query = select(SmokeRun.id).where(SmokeRun.kind == kind)
    if city is not None:
        query = query.where(func.lower(SmokeRun.city) == city.strip().lower())
    return session.scalars(query.order_by(SmokeRun.run_at.desc(), SmokeRun.id.desc()).limit(1)).first()


def load_run(session: Session, run_id: int, with_trails: bool = True) -> dict[str, Any] | None:
    """A run as a dict. `trails_json` is the trails column as JSON text (not parsed), so a route
    can pass megabytes of trails through to the response without decoding and re-encoding them."""
    trails = "trails::text AS trails_json" if with_trails else "NULL::text AS trails_json"
    row = session.execute(
        text(
            f"SELECT id, kind, city, run_at, t0, hours, met_run_ids, params, summary, receptors,"
            f" {trails} FROM smoke_run WHERE id = :id"
        ),
        {"id": run_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def latest_back_summaries(session: Session) -> list[dict[str, Any]]:
    """The newest back run of every city (summaries only, no trails)."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (lower(city)) id, city, run_at, t0, hours, summary, receptors, params
            FROM smoke_run WHERE kind = 'back'
            ORDER BY lower(city), run_at DESC, id DESC
            """
        )
    ).mappings()
    return [dict(r) for r in rows]
