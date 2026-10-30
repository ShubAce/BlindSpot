"""Fire tables: NASA FIRMS detections, which days of FIRMS history we hold in full, and the official
CREAMS (ICAR-IARI) daily event layers that India's government counts.

- fire_detection: one row per FIRMS detection (architecture.md section 4), from the keyless NRT
  files or the MAP_KEY area API. Both feeds carry identical rows, and the unique key
  (source, acq_utc, lat, lon) keeps exactly one.
- fire_coverage: one row per (FIRMS source, UTC day) fetched through the area API, with
  `complete` once the fetch happened a full day after the day ended. It lets a chart tell
  "0 fires that day" from "we never fetched that day".
- official_fire_event / official_layer: the CREAMS "rice residue burning events" layer for each IST
  day, stored as published. A day is replaced as a whole when its layer is fetched again, so
  corrections on CREAMS' side flow through.

State and district come from our own polygons (blindspot.geo), filled after insert; they stay
blank until the boundaries module has loaded them, and are never guessed.
"""

from __future__ import annotations

from datetime import date, datetime

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class FireDetection(Base):
    """One NASA FIRMS active-fire detection: a 375 m (VIIRS) or ~1 km (MODIS) pixel that was hot
    when a polar-orbiting satellite passed over. Not a field and not a fire event: three VIIRS
    satellites can see the same fire within an hour, and each look is its own row."""

    __tablename__ = "fire_detection"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # VIIRS_NOAA20_NRT / ..._NOAA21_ / ..._SNPP_ / MODIS_NRT
    satellite: Mapped[str] = mapped_column(String(16))  # Suomi NPP, NOAA-20, NOAA-21, Aqua, Terra
    instrument: Mapped[str] = mapped_column(String(8))  # VIIRS / MODIS
    acq_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # overpass time, UTC
    lat: Mapped[float] = mapped_column(Double)  # pixel centre
    lon: Mapped[float] = mapped_column(Double)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    confidence: Mapped[str] = mapped_column(String(8))  # low / nominal / high (MODIS % classed)
    confidence_pct: Mapped[int | None] = mapped_column(Integer)  # MODIS only, 0-100
    frp_mw: Mapped[float | None] = mapped_column(Double)  # fire radiative power, MW
    bright_ti4: Mapped[float | None] = mapped_column(Double)  # K, VIIRS I4 / MODIS 4 um channel
    bright_ti5: Mapped[float | None] = mapped_column(Double)  # K, VIIRS I5 / MODIS 11 um channel
    scan: Mapped[float | None] = mapped_column(Double)  # pixel size across track, km
    track: Mapped[float | None] = mapped_column(Double)  # pixel size along track, km
    daynight: Mapped[str] = mapped_column(String(1))  # D / N, as FIRMS flags it
    version: Mapped[str | None] = mapped_column(String(16))  # e.g. 2.0NRT
    state: Mapped[str | None] = mapped_column(Text)  # from our polygons; null outside them
    district: Mapped[str | None] = mapped_column(Text)
    admin_version: Mapped[str | None] = mapped_column(String(32))  # boundary data it was labelled with
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source", "acq_utc", "lat", "lon", name="uq_fire_detection_key"),
        CheckConstraint("confidence IN ('low', 'nominal', 'high')", name="ck_fire_confidence"),
        CheckConstraint("daynight IN ('D', 'N')", name="ck_fire_daynight"),
        Index("ix_fire_detection_acq_utc", "acq_utc"),
        Index("ix_fire_detection_state_acq_utc", "state", "acq_utc"),
    )


class FireStaticSite(Base):
    """A persistent heat source: VIIRS/MODIS saw heat within 400 m on 3 or more different nights.
    Crop residue is burnt once and in daylight; a spot hot night after night is a kiln, a factory
    or a flare (FIRMS's own static-thermal-anomaly idea; research doc section 6.4). Detections
    inside `area` are left out of crop-fire counts unless a caller asks for them. Recomputed from
    all stored detections after every ingest (jobs/firms.py)."""

    __tablename__ = "fire_static_site"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    area: Mapped[WKBElement] = mapped_column(Geometry("MULTIPOLYGON", srid=4326))  # 400 m zones
    lat: Mapped[float] = mapped_column(Double)  # centroid of the night detections
    lon: Mapped[float] = mapped_column(Double)
    nights: Mapped[int] = mapped_column(Integer)  # distinct IST nights with a night detection
    night_detections: Mapped[int] = mapped_column(Integer)
    first_night: Mapped[date] = mapped_column(Date)
    last_night: Mapped[date] = mapped_column(Date)
    state: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FireCoverage(Base):
    """Which UTC days of each FIRMS source the area API has given us, and whether that day was
    complete when fetched (fetched at least a full day after it ended, so no NRT rows can still
    arrive). Days fetched only through the keyless 24 h files are not listed here."""

    __tablename__ = "fire_coverage"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)  # UTC day (FIRMS acq_date)
    rows: Mapped[int] = mapped_column(Integer)  # detections the API returned that day, AOI box
    complete: Mapped[bool] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL")
    )


class OfficialFireEvent(Base):
    """One event of a CREAMS (ICAR-IARI) daily residue-burning layer: the count India officially
    reports. Times arrive in IST and are stored in UTC like everything else."""

    __tablename__ = "official_fire_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    layer: Mapped[str] = mapped_column(String(16))  # "rice" (paddy residue season layer)
    layer_date: Mapped[date] = mapped_column(Date)  # the IST day the layer covers
    satellite: Mapped[str] = mapped_column(String(16))  # our name: Suomi NPP, Aqua, ...
    satellite_raw: Mapped[str] = mapped_column(String(32))  # as CREAMS writes it: S-NPP, AQUA
    acq_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lat: Mapped[float] = mapped_column(Double)
    lon: Mapped[float] = mapped_column(Double)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    frp_mw: Mapped[float | None] = mapped_column(Double)  # CREAMS "fire_power"
    daynight: Mapped[str | None] = mapped_column(String(1))  # CREAMS "day_flag"
    creams_district: Mapped[str | None] = mapped_column(Text)  # as CREAMS names it (upper case)
    creams_block: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)  # from our polygons, like fire_detection
    district: Mapped[str | None] = mapped_column(Text)
    admin_version: Mapped[str | None] = mapped_column(String(32))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "layer", "layer_date", "satellite_raw", "acq_utc", "lat", "lon",
            name="uq_official_fire_event_key",
        ),  # fmt: skip
        Index("ix_official_fire_event_date", "layer", "layer_date"),
    )


class OfficialLayer(Base):
    """When each CREAMS daily layer was last fetched and how many events it held. `final` once
    fetched a full day after the IST day ended; until then the layer may still grow."""

    __tablename__ = "official_layer"

    layer: Mapped[str] = mapped_column(String(16), primary_key=True)
    layer_date: Mapped[date] = mapped_column(Date, primary_key=True)
    events: Mapped[int] = mapped_column(Integer)
    published: Mapped[bool] = mapped_column(Boolean)  # False: CREAMS had no such layer (yet)
    final: Mapped[bool] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL")
    )
