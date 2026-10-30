"""The tle table: the latest CelesTrak orbital elements of each satellite we follow.

One row per satellite, replaced only by a newer element set. Pass predictions (analytics/passes.py,
/api/orbits/passes) and the browser's live satellite tracks (/api/orbits/tle -> satellite.js) all
start from these rows, so the server and the map always use the same orbit.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class Tle(Base):
    """A two-line element set (TLE) as CelesTrak published it, verbatim."""

    __tablename__ = "tle"

    norad_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    key: Mapped[str] = mapped_column(String(16), unique=True)  # snpp, noaa20, aqua, s2a, ...
    name: Mapped[str] = mapped_column(String(32))  # Suomi NPP, NOAA-20, Aqua, Sentinel-2A, ...
    object_name: Mapped[str] = mapped_column(String(64))  # CelesTrak's name, e.g. NOAA 20 (JPSS-1)
    instrument: Mapped[str] = mapped_column(String(8))  # VIIRS / MODIS / MSI
    line1: Mapped[str] = mapped_column(String(69))
    line2: Mapped[str] = mapped_column(String(69))
    epoch: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the elements are exact
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
