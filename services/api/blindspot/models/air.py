"""Air-quality tables, and how we name a CPCB monitoring station and its city.

air_obs holds every air-quality number we serve, one row per (provider, station, parameter, time):
- provider "openaq"      CPCB-network stations via OpenAQ v3, concentrations in µg/m³
                         (period_minutes 15 = a station's latest 15-minute reading, 60 = an
                         hourly mean);
- provider "datagov_aqi" the official CPCB feed on data.gov.in. Its values are NAQI *sub-indices*
                         (0-500), not concentrations, so they are stored as `pm25_subindex` etc. and
                         can never be mixed up with µg/m³ by a query that asks for `pm25`;
- provider "cams"        the Copernicus CAMS model at each city (is_model = true), never averaged
                         together with measurements.
The provider values are the source keys of provenance.SOURCE_REGISTRY, so a row maps to its source.

dss_snapshot keeps every distinct version of IITM's Decision Support System numbers: the daily
share of Delhi's PM2.5 that IITM attributes to stubble burning (our validation target).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base

# The `provider` column, one per upstream source (keys of provenance.SOURCE_REGISTRY).
OPENAQ = "openaq"
DATAGOV = "datagov_aqi"
CAMS = "cams"

UGM3 = "µg/m³"
SUBINDEX_UNIT = "NAQI sub-index"

# How long a reading counts as "now". Older stations are still listed, labelled stale.
FRESH_MINUTES = 120


class AirObs(Base):
    """One air-quality value: a station measurement, a CPCB sub-index, or a model value."""

    __tablename__ = "air_obs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))  # openaq / datagov_aqi / cams
    station_id: Mapped[str] = mapped_column(Text)  # OpenAQ location id, CPCB station name, or city
    station_name: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Double)
    lon: Mapped[float] = mapped_column(Double)
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    parameter: Mapped[str] = mapped_column(String(32))  # pm25, pm10, or <pollutant>_subindex
    value: Mapped[float] = mapped_column(Double)
    unit: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # end of the period, UTC
    # Length of the averaging period behind `value`: 15 (a 15-minute reading), 60 (an hourly mean
    # or an hourly model step), 480 / 1440 (CPCB's 8 h / 24 h sub-index windows). Part of the key,
    # so an hourly mean and the 15-minute reading that ends the same hour can both be kept.
    period_minutes: Mapped[int] = mapped_column(Integer)
    is_model: Mapped[bool] = mapped_column(Boolean, default=False)
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "station_id",
            "parameter",
            "observed_at",
            "period_minutes",
            name="uq_air_obs_key",
        ),
        Index("ix_air_obs_city_param_time", "city", "parameter", "observed_at"),
        Index("ix_air_obs_observed_at", "observed_at"),
    )


class DssSnapshot(Base):
    """One distinct version of the IITM DSS stubble-share numbers (deduplicated by content hash).

    IITM publishes them on a web page, not an API, and may revise them; keeping each version
    (with when we first and last saw it) lets us show exactly what was published when.
    """

    __tablename__ = "dss_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    run_date: Mapped[date] = mapped_column(Date)  # first day of the 4-day outlook = DSS run date
    daily_pct: Mapped[dict[str, Any]] = mapped_column(JSONB)  # {"2026-10-30": 1.749, ...}
    forecast_pct: Mapped[dict[str, Any]] = mapped_column(JSONB)  # {day: {source: pct}}
    basis: Mapped[str | None] = mapped_column(Text)  # IITM's own sentence on how it is derived
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The newest read that returned this version (so a response cites the latest confirmation).
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )


# --- CPCB station names -------------------------------------------------------------------------
# CPCB names a station "<site>, <city> - <agency>", e.g. "Golden Temple, Amritsar - PPCB" or
# "Knowledge Park - III, Greater Noida - UPPCB". OpenAQ and data.gov.in both use these names, with
# small differences ("Anand Vihar, New Delhi - DPCC" vs "Anand Vihar, Delhi - DPCC", ", UP" added,
# the agency of an IMD station given as IITM). The helpers below turn a name into comparable parts.

# CPCB's own feed files every station in the NCT under city "Delhi" (59 of 59 on 2026-10-30), so
# the "New Delhi" some OpenAQ names carry is the same city for our purposes.
CITY_ALIASES = {"new delhi": "Delhi", "gurgaon": "Gurugram"}

_AGENCY = re.compile(r"^(?P<site>.*?)\s*-\s*(?P<agency>[A-Za-z][A-Za-z .]*?)\s*$")
_STATE_CODE = re.compile(r"^[A-Z]{2}$")  # ", UP" after the city in some OpenAQ names


def split_station_name(name: str) -> tuple[str, str | None, str | None]:
    """(site, city, agency) from a CPCB station name; city and agency are None when absent.

    >>> split_station_name("Sector - 125, Noida, UP - UPPCB")
    ('Sector - 125, Noida', 'Noida', 'UPPCB')
    """
    text = " ".join(name.split())
    match = _AGENCY.match(text)
    site, agency = (match["site"], match["agency"]) if match else (text, None)
    parts = [p.strip() for p in site.split(",") if p.strip()]
    if len(parts) > 2 and _STATE_CODE.match(parts[-1]):
        parts = parts[:-1]  # drop the state code: "..., Noida, UP"
    site = ", ".join(parts)
    city = canonical_city(re.sub(r"\(.*?\)", "", parts[-1])) if len(parts) > 1 else None
    return site, city, agency


def canonical_city(city: str | None) -> str | None:
    """A city name as CPCB's feed spells it ("New Delhi" -> "Delhi"), whitespace tidied."""
    if city is None:
        return None
    tidy = " ".join(city.split())
    return CITY_ALIASES.get(tidy.lower(), tidy) or None


def site_key(name: str) -> str:
    """A comparison key for the site part of a station name: lower-case letters and digits only,
    "New Delhi" read as "Delhi", the agency dropped (it differs between feeds for some stations)."""
    site, _city, _agency = split_station_name(name)
    text = re.sub(r"\(.*?\)", "", site.lower())
    text = re.sub(r"\bnew delhi\b", "delhi", text)
    return re.sub(r"[^a-z0-9]", "", text)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine, mean Earth radius 6371.0088 km, IUGG)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class StationRef:
    """A station as the official CPCB feed (data.gov.in) names and places it."""

    name: str
    city: str | None
    state: str | None
    lat: float
    lon: float


class CpcbIndex:
    """Finds the official CPCB feed's entry for a station named by another feed (OpenAQ).

    Match on the site part of the name (see site_key). When a site has two stations, as Pusa does
    (one run by DPCC, one by IMD/IITM), prefer the same agency, else the nearest. Tested on the
    2026-10-30 feeds: 95 of 98 live OpenAQ stations match one entry, 2 resolve this way, and 1
    ("Ratanpura, Rupnagar - Ambuja Cements") has no counterpart in CPCB's feed.
    """

    def __init__(self, stations: Iterable[StationRef]) -> None:
        self.by_site: dict[str, list[StationRef]] = {}
        self.city_state: dict[str, str] = {}
        for station in stations:
            self.by_site.setdefault(site_key(station.name), []).append(station)
            if station.city and station.state:
                self.city_state.setdefault(station.city.lower(), station.state)

    def match(self, name: str, lat: float, lon: float) -> StationRef | None:
        """The best entry for one station (enough to name its city and state)."""
        candidates = self.by_site.get(site_key(name), [])
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        agency = split_station_name(name)[2]
        same = [c for c in candidates if split_station_name(c.name)[2] == agency] or candidates
        return min(same, key=lambda c: distance_km(lat, lon, c.lat, c.lon))

    def match_all(self, stations: Iterable[tuple[str, str, float, float]]) -> dict[str, StationRef]:
        """One-to-one matches for many (key, name, lat, lon) stations, so no CPCB entry (and its
        AQI) is given to two stations. First pass: same site and same agency; second pass: same
        site, nearest entry still free. Example: data.gov.in files every IMD-run station under
        "IITM", and Lodhi Road has both an IITM and an IMD station: OpenAQ's IITM one takes the
        IITM entry in pass 1, and its IMD one is left unmatched rather than given its
        neighbour's."""
        todo = list(stations)
        taken: set[str] = set()
        out: dict[str, StationRef] = {}
        for same_agency in (True, False):
            for key, name, lat, lon in todo:
                if key in out:
                    continue
                agency = split_station_name(name)[2]
                free = [
                    c
                    for c in self.by_site.get(site_key(name), [])
                    if c.name not in taken
                    and (not same_agency or split_station_name(c.name)[2] == agency)
                ]
                if free:
                    best = min(free, key=lambda c: distance_km(lat, lon, c.lat, c.lon))
                    out[key] = best
                    taken.add(best.name)
        return out

    def label(self, name: str, lat: float, lon: float) -> tuple[str | None, str | None]:
        """(city, state) for a station: from its CPCB entry, else the city in its name and the
        state CPCB files that city under. None where neither knows; never a guess."""
        found = self.match(name, lat, lon)
        if found is not None:
            return canonical_city(found.city), found.state
        city = split_station_name(name)[1]
        return city, self.city_state.get(city.lower()) if city else None
