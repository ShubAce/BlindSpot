"""Tables of the geostationary fire watch (GK-2A), filled by jobs/geostationary.py.

- geo_fire_slot: one row per 10-minute GK-2A scan we analysed: how many fire pixels it saw inside
  Punjab and Haryana (clipped to India's state polygons, never a lat/lon box), how much of the
  region was clear, and whether the scan could be read at all. This is the diurnal record.
- geo_fire_pixel: every fire pixel of those scans, with its footprint (~12 km2 at Punjab's
  66 degree view angle) and the measurements that made it a fire, so each one can be audited.
- geo_fire_validation: one row per polar-satellite look (VIIRS / MODIS pass) at the region that
  had a GK-2A scan within a few minutes: how many of the polar detections GK-2A also flagged and
  how many GK-2A pixels the polar satellite confirmed. Pooled, these are the hit and false-alarm
  rates every geostationary response carries.
- geo_fire_validation_coverage: the same comparisons counted only where the polar sensor viewed
  the ground within a fixed scan angle (its swath edge sees only large fires), and by scan angle.
- geo_fire_static: GK-2A pixels flagged as persistent (non-crop) heat sources, and why. The job
  rebuilds it on every run; the fire test leaves these pixels out (geofire.detect_fires `exclude`).
- geo_fire_static_pixel: what the fire test still found on those flagged pixels, scan by scan
  (same columns as geo_fire_pixel). Kept apart, not deleted: the counts leave them out by default,
  `include_static=true` shows them, and the night rule needs them to see a source still burning.

Counts are fire *pixels* per scan: an activity index, never a number of fires.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class GeoFireSlot(Base):
    """One analysed GK-2A full-disk scan (status 'ok') or one we could not read ('failed')."""

    __tablename__ = "geo_fire_slot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slot_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )  # nominal scan start
    t_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # when it saw Punjab
    algorithm: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # ok / failed
    count: Mapped[int | None] = mapped_column(
        Integer
    )  # fire pixels in Punjab + Haryana
    count_pb: Mapped[int | None] = mapped_column(Integer)
    count_hr: Mapped[int | None] = mapped_column(Integer)
    count_high: Mapped[int | None] = mapped_column(Integer)
    frp_sum_mw: Mapped[float | None] = mapped_column(
        Float
    )  # top-of-atmosphere lower bound
    clear_frac: Mapped[float | None] = mapped_column(
        Float
    )  # share of region pixels seen clear
    region_pixels: Mapped[int | None] = mapped_column(Integer)
    sza_deg: Mapped[float | None] = mapped_column(
        Float
    )  # median solar zenith over the region
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("slot_utc", "algorithm", name="uq_geo_fire_slot"),
        Index("ix_geo_fire_slot_t", "t_utc"),
    )


class _FirePixelColumns:
    """The columns of a GK-2A pixel that passed the fire test (shared by two tables)."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slot_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    t_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    algorithm: Mapped[str] = mapped_column(String(32))
    row: Mapped[int] = mapped_column(
        Integer
    )  # full-disk 2 km grid line (0 = north edge)
    col: Mapped[int] = mapped_column(Integer)
    lon: Mapped[float]
    lat: Mapped[float]
    state: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(SmallInteger)  # 1 nominal, 2 high
    bt38_k: Mapped[float | None] = mapped_column(Float)
    bt11_k: Mapped[float | None] = mapped_column(Float)
    btd_k: Mapped[float | None] = mapped_column(Float)
    excess_k: Mapped[float | None] = mapped_column(Float)  # BTD above its background
    frp_mw: Mapped[float | None] = mapped_column(Float)
    area_km2: Mapped[float | None] = mapped_column(Float)
    absolute: Mapped[bool] = mapped_column(default=False)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    footprint: Mapped[WKBElement | None] = mapped_column(Geometry("POLYGON", srid=4326))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )


class GeoFirePixel(_FirePixelColumns, Base):
    """One GK-2A pixel that passed the fire test, inside Punjab or Haryana."""

    __tablename__ = "geo_fire_pixel"

    __table_args__ = (
        UniqueConstraint(
            "slot_utc", "algorithm", "row", "col", name="uq_geo_fire_pixel"
        ),
        Index("ix_geo_fire_pixel_slot", "slot_utc"),
    )


class GeoFireValidationCoverage(Base):
    """The same comparison as a geo_fire_validation row (same algorithm, satellite and pass start),
    counted only where the polar sensor could see: region pixels it viewed within max_scan_deg of
    nadir (scan angle at the satellite, from its orbital elements in the tle table). Beyond that
    its pixels grow so much that missing a GK-2A fire says more about the reference than about
    GK-2A. by_scan_angle holds the counts in bins of that angle (geofire.SCAN_ANGLE_BINS)."""

    __tablename__ = "geo_fire_validation_coverage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(32))
    satellite: Mapped[str] = mapped_column(String(32))
    instrument: Mapped[str] = mapped_column(String(16))
    pass_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    regime: Mapped[str] = mapped_column(String(16))
    max_scan_deg: Mapped[float] = mapped_column(Float)
    tle_epoch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    region_pixels: Mapped[int] = mapped_column(Integer)
    covered_pixels: Mapped[int] = mapped_column(Integer)  # region pixels within max_scan_deg
    ref_pixels: Mapped[int] = mapped_column(Integer)
    ref_hits: Mapped[int] = mapped_column(Integer)
    ref_pixels_strong: Mapped[int] = mapped_column(Integer)
    ref_hits_strong: Mapped[int] = mapped_column(Integer)
    ref_pixels_clear: Mapped[int] = mapped_column(Integer)
    geo_pixels: Mapped[int] = mapped_column(Integer)
    geo_hits: Mapped[int] = mapped_column(Integer)
    by_scan_angle: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "algorithm", "satellite", "pass_start_utc", name="uq_geo_fire_validation_coverage"
        ),
    )


class GeoFireStatic(Base):
    """A GK-2A pixel flagged as a persistent heat source (a plant, kiln or flare, not a field),
    by one rule. The job replaces the table on every run (tens of rows)."""

    __tablename__ = "geo_fire_static"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    row: Mapped[int] = mapped_column(Integer)  # full-disk 2 km grid line
    col: Mapped[int] = mapped_column(Integer)
    rule: Mapped[str] = mapped_column(String(16))  # firms / gk2a_night
    lon: Mapped[float]
    lat: Mapped[float]
    nights: Mapped[int] = mapped_column(Integer)  # distinct nights of the evidence
    detections: Mapped[int] = mapped_column(Integer)
    first_night: Mapped[date | None] = mapped_column(Date)
    last_night: Mapped[date | None] = mapped_column(Date)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    footprint: Mapped[WKBElement | None] = mapped_column(Geometry("POLYGON", srid=4326))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("row", "col", "rule", name="uq_geo_fire_static"),)


class GeoFireStaticPixel(_FirePixelColumns, Base):
    """A pixel that passed the fire test but is flagged in geo_fire_static: kept, not counted."""

    __tablename__ = "geo_fire_static_pixel"

    rules: Mapped[str] = mapped_column(String(32))  # the geo_fire_static rules, comma-separated

    __table_args__ = (
        UniqueConstraint(
            "slot_utc", "algorithm", "row", "col", name="uq_geo_fire_static_pixel"
        ),
        Index("ix_geo_fire_static_pixel_slot", "slot_utc"),
        Index("ix_geo_fire_static_pixel_t", "t_utc"),
    )


class GeoFireValidation(Base):
    """GK-2A against one polar-satellite pass over Punjab + Haryana (on the GK-2A grid)."""

    __tablename__ = "geo_fire_validation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(32))
    satellite: Mapped[str] = mapped_column(String(32))
    instrument: Mapped[str] = mapped_column(String(16))
    pass_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pass_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )  # the GK-2A scan used
    dt_min: Mapped[float]  # GK-2A scan time minus pass mid-time, minutes
    regime: Mapped[str] = mapped_column(
        String(16)
    )  # day / night (solar zenith at the region)
    sza_deg: Mapped[float | None] = mapped_column(Float)
    clear_frac: Mapped[float | None] = mapped_column(Float)
    n_detections: Mapped[int] = mapped_column(Integer)  # polar detections in the region
    ref_pixels: Mapped[int] = mapped_column(Integer)
    ref_hits: Mapped[int] = mapped_column(Integer)
    ref_pixels_strong: Mapped[int] = mapped_column(Integer)
    ref_hits_strong: Mapped[int] = mapped_column(Integer)
    ref_pixels_clear: Mapped[int] = mapped_column(Integer)
    geo_pixels: Mapped[int] = mapped_column(Integer)
    geo_hits: Mapped[int] = mapped_column(Integer)
    strong_frp_mw: Mapped[float] = mapped_column(Float)
    reference: Mapped[str] = mapped_column(String(32))  # fire_detection or firms_csv
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "algorithm", "satellite", "pass_start_utc", name="uq_geo_fire_validation"
        ),
    )
