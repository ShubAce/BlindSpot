"""Foundation tables: source_fetch (provenance of every upstream call), boundary and place (how
we name a location), and job_run (when each job last ran and whether it worked). SRID 4326."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class SourceFetch(Base):
    """One row per upstream call (HTTP or SDK), success or failure.

    This is the audit trail behind every number we show. `url` never holds a secret: keys are
    replaced by *** before the row is written.
    """

    __tablename__ = "source_fetch"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64))  # a key of provenance.SOURCE_REGISTRY
    url: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool]
    http_status: Mapped[int | None]
    records: Mapped[int | None]  # rows parsed from the response; the caller sets it
    latency_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_source_fetch_key_started", "source_key", "started_at"),
        Index(
            "ix_source_fetch_key_ok_started",
            "source_key",
            "started_at",
            postgresql_where=text("ok"),
        ),
    )


class Boundary(Base):
    """An administrative area: a state, district or subdistrict polygon."""

    __tablename__ = "boundary"

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))  # state / district / subdistrict
    name: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)  # state it lies in (its own name for a state)
    district: Mapped[str | None] = mapped_column(Text)  # parent district, for subdistrict rows
    code: Mapped[str | None] = mapped_column(String(32))  # official code (LGD / census) if known
    geom: Mapped[WKBElement] = mapped_column(Geometry("MULTIPOLYGON", srid=4326))
    source: Mapped[str] = mapped_column(Text)  # dataset name and version
    props: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # e.g. names in Hindi / Punjabi
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("level IN ('state', 'district', 'subdistrict')", name="ck_boundary_level"),
        Index("ix_boundary_level_state_name", "level", "state", "name"),
    )


class Place(Base):
    """A named settlement (village / town / city) as a point, used to name the nearest place."""

    __tablename__ = "place"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # village / town / city
    name: Mapped[str] = mapped_column(Text)
    subdistrict: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Double)
    lon: Mapped[float] = mapped_column(Double)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    population: Mapped[int | None]
    source: Mapped[str] = mapped_column(Text)
    props: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_place_kind_name", "kind", "name"),
        Index("ix_place_state_district", "state", "district"),
    )


class JobRun(Base):
    """One row per job run (scheduled, chained or from the CLI): when, how long, and any error.

    The scheduler reads it at start-up, so a restart does not re-run a job that ran a minute ago.
    """

    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(16))  # schedule / chain / cli / manual
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool]
    duration_ms: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[Any] = mapped_column(JSONB, nullable=True)  # what the job returned, as JSON

    __table_args__ = (Index("ix_job_run_name_started", "name", "started_at"),)
