"""risk_run: one row per scoring run of the `risk` job (blindspot/jobs/risk.py).

What every field of one run shares (model, weights, the season, today's typical counts, the upstream
fetch ids...) is stored here once, in `factors`; each harvest_cluster row keeps only its own numbers
({"run": id, "v": "[...]"}, blindspot/analytics/risk.py ROW_VALUES). The run's backtest report lives
here too, so a serverless API (no shared disk) can serve it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from blindspot.db import Base


class RiskRun(Base):
    __tablename__ = "risk_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str] = mapped_column(String(32))
    fields: Mapped[int] = mapped_column(Integer)  # fields scored
    changed: Mapped[int | None] = mapped_column(Integer)  # rows rewritten (the rest already held these numbers)
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB)  # analytics.risk.run_constants()
    backtest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the job's backtest report
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_risk_run_as_of", "as_of"),)


def latest_run(session: Session) -> dict[str, Any] | None:
    """The newest run as {id, as_of, model, factors}, or None (none yet, or no table yet)."""
    if not session.execute(text("SELECT to_regclass('public.risk_run') IS NOT NULL")).scalar_one():
        return None
    row = session.execute(text(
        "SELECT id, as_of, model, factors FROM risk_run ORDER BY as_of DESC, id DESC LIMIT 1"
    )).mappings().first()
    return dict(row) if row else None


def latest_backtest(session: Session) -> dict[str, Any] | None:
    """The newest stored backtest report, or None."""
    if not session.execute(text("SELECT to_regclass('public.risk_run') IS NOT NULL")).scalar_one():
        return None
    return session.execute(text(
        "SELECT backtest FROM risk_run WHERE backtest IS NOT NULL ORDER BY as_of DESC, id DESC LIMIT 1"
    )).scalar()


def latest_backtest_summary(session: Session) -> dict[str, Any] | None:
    """The newest backtest report without its per-window list (which the API only counts), plus
    `windows_count`: what GET /api/risk serves, at a fraction of the bytes from the database."""
    if not session.execute(text("SELECT to_regclass('public.risk_run') IS NOT NULL")).scalar_one():
        return None
    row = session.execute(text("""
        SELECT backtest - 'windows', coalesce(jsonb_array_length(backtest -> 'windows'), 0)
        FROM risk_run WHERE backtest IS NOT NULL ORDER BY as_of DESC, id DESC LIMIT 1
    """)).first()
    if row is None:
        return None
    return {**row[0], "windows_count": int(row[1])}
