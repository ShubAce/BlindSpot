"""Sentinel-2 tables: the scenes we catalogued (s2_scene), the burn scars found between clear passes
(burn_cluster) and the harvested fields still waiting with unburnt residue (harvest_cluster).

Everything is SRID 4326. Dates of passes are stored as exact UTC sensing times (timestamptz) so a
scar's burn window can be matched against fire detections to the minute.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class S2Scene(Base):
    """One Sentinel-2 L2A item (one MGRS tile, one pass) found in the STAC catalogue.

    status: `catalogued` (passed the scene filter, waiting), `skipped` (too cloudy or almost all
    nodata, `error` says why), `processed` (its pixels were compared with the previous clear pass),
    `failed` (it failed the plausibility gate or could not be read; `error` says why).
    """

    __tablename__ = "s2_scene"

    item_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    collection: Mapped[str] = mapped_column(String(64))
    datetime: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))  # tile sensing time, UTC
    obs_date: Mapped[dt.date] = mapped_column(Date)  # UTC date: all items of one tile on one date are one pass
    mgrs_tile: Mapped[str] = mapped_column(String(8))  # e.g. "43REQ"
    platform: Mapped[str | None] = mapped_column(String(16))  # sentinel-2a / 2b / 2c
    relative_orbit: Mapped[int | None] = mapped_column(Integer)
    cloud_pct: Mapped[float | None] = mapped_column(Double)  # eo:cloud_cover of the whole tile
    nodata_pct: Mapped[float | None] = mapped_column(Double)  # s2:nodata_pixel_percentage (swath edge)
    bbox: Mapped[list[float] | None] = mapped_column(ARRAY(Double))  # [min_lon, min_lat, max_lon, max_lat]
    geom: Mapped[WKBElement | None] = mapped_column(Geometry("GEOMETRY", srid=4326))  # data footprint
    status: Mapped[str] = mapped_column(String(16), default="catalogued")
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # gate/haze numbers per processing run
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (Index("ix_s2_scene_tile_date", "mgrs_tile", "obs_date"),)


class BurnCluster(Base):
    """A patch of cropland that burnt between two clear Sentinel-2 passes of the same tile.

    burn_after / burn_before are the sensing times of the clear pass before the burn and the first
    pass that shows it burnt. unseen is NULL while no fire-detection record covers that window
    (we never call a scar "unseen" without having looked).
    """

    __tablename__ = "burn_cluster"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POLYGON", srid=4326))
    centroid: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    area_ha: Mapped[float] = mapped_column(Double)
    n_px: Mapped[int] = mapped_column(Integer)  # pixels at res_m
    res_m: Mapped[int] = mapped_column(Integer)  # 20 in hotspot districts, coarser elsewhere
    mgrs_tile: Mapped[str] = mapped_column(String(8))
    scope: Mapped[str] = mapped_column(String(16))  # processing stage: hotspots / punjab / haryana
    block: Mapped[str] = mapped_column(String(24))  # processing block holding its anchor point, "r1024_c2048"
    burn_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    burn_before: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    gap_days: Mapped[float] = mapped_column(Double)
    scene_pre: Mapped[str] = mapped_column(String(80))
    scene_post: Mapped[str] = mapped_column(String(80))
    collection: Mapped[str] = mapped_column(String(64))
    hazy: Mapped[bool] = mapped_column(Boolean, default=False)  # post pass flagged as smoke-hazy
    # Cluster means of the evidence (reflectance 0-1; d* = pre minus post)
    dnbr: Mapped[float | None] = mapped_column(Double)
    nbr_post: Mapped[float | None] = mapped_column(Double)
    d_b8a: Mapped[float | None] = mapped_column(Double)
    d_b11: Mapped[float | None] = mapped_column(Double)
    d_b12: Mapped[float | None] = mapped_column(Double)
    bais2_post: Mapped[float | None] = mapped_column(Double)
    tbi_post: Mapped[float | None] = mapped_column(Double)
    ndvi_pre: Mapped[float | None] = mapped_column(Double)
    ndvi_post: Mapped[float | None] = mapped_column(Double)
    confidence: Mapped[float] = mapped_column(Double)
    # Fire matching (re-run every job run, so late-arriving fire data updates it; a row is rewritten
    # only when its verdict changes, so matched_at is when the current verdict was set)
    unseen: Mapped[bool | None] = mapped_column(Boolean)
    fire_matches: Mapped[int | None] = mapped_column(Integer)
    nearest_fire_m: Mapped[float | None] = mapped_column(Double)  # within 5 km, any time in the window
    matched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Where it is (from the boundary and place tables; blank until they are loaded)
    state: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    subdistrict: Mapped[str | None] = mapped_column(Text)
    village: Mapped[str | None] = mapped_column(Text)
    village_km: Mapped[float | None] = mapped_column(Double)
    model_version: Mapped[str] = mapped_column(String(48))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_burn_cluster_tile_post", "mgrs_tile", "scope", "burn_before"),
        Index("ix_burn_cluster_window", "burn_after", "burn_before"),
        Index("ix_burn_cluster_district", "district"),
    )


class HarvestCluster(Base):
    """A patch of cropland harvested this season whose residue was still unburnt (not burnt, not
    tilled, not sown) on the latest clear pass. A snapshot: rebuilt for a tile on every run.

    risk_score and risk_factors are left NULL here; the risk module fills them.
    """

    __tablename__ = "harvest_cluster"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # No spatial index on geom: every spatial query on this table goes through centroid (GET /api/risk,
    # the risk job, place labels), and the unused index was 8 MB of a 0.5 GB hosted database.
    geom: Mapped[WKBElement] = mapped_column(Geometry("POLYGON", srid=4326, spatial_index=False))
    centroid: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    area_ha: Mapped[float] = mapped_column(Double)
    n_px: Mapped[int] = mapped_column(Integer)
    res_m: Mapped[int] = mapped_column(Integer)
    mgrs_tile: Mapped[str] = mapped_column(String(8))
    scope: Mapped[str] = mapped_column(String(16))
    harvested_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))  # last pass with standing crop
    harvested_before: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))  # first pass harvested
    last_seen_unburnt: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    scene_last: Mapped[str] = mapped_column(String(80))
    collection: Mapped[str] = mapped_column(String(64))
    ndvi_last: Mapped[float | None] = mapped_column(Double)
    state: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    subdistrict: Mapped[str | None] = mapped_column(Text)
    village: Mapped[str | None] = mapped_column(Text)
    village_km: Mapped[float | None] = mapped_column(Double)
    risk_score: Mapped[float | None] = mapped_column(Double)
    risk_factors: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(String(48))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_harvest_cluster_tile", "mgrs_tile", "scope"),)
