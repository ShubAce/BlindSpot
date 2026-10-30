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

Counts are fire *pixels* per scan: an activity index, never a number of fires.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    BigInteger,
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


class GeoFirePixel(Base):
    """One GK-2A pixel that passed the fire test, inside Punjab or Haryana."""

    __tablename__ = "geo_fire_pixel"

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

    __table_args__ = (
        UniqueConstraint(
            "slot_utc", "algorithm", "row", "col", name="uq_geo_fire_pixel"
        ),
        Index("ix_geo_fire_pixel_slot", "slot_utc"),
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
