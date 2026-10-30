"""CelesTrak GP orbital elements (TLEs) for the satellites BlindSpot follows. Pure fetch + parse.

CelesTrak is a free service run by one person, with a strict usage policy (read 2026-10-30,
celestrak.org/NORAD/documentation/gp-data-formats.php and usage-policy.php):
- GP data is updated once every 2 hours; download each update once. Repeat downloads may get
  HTTP 403 "GP data has not updated since your last successful download".
- More than 50 HTTP errors (301/403/404) in 2 hours puts the IP on a firewall for manual review,
  and automated clients must stop at the first non-200 answer. So: one query per satellite, the
  canonical URL (a 301 means an outdated client), never a retry on 4xx (blindspot.http.fetch
  does not retry 4xx), and the job stops at the first failure.
- Always pass FORMAT: the default changed to CSV on 2026-05-09.

We ask for FORMAT=TLE and keep CelesTrak's two lines verbatim (the API contract serves them as
line1/line2); orbit_mean_elements() derives the equivalent OMM fields for satellite.js json2satrec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sgp4 import exporter
from sgp4.api import Satrec

from blindspot.http import FetchResult, fetch
from blindspot.provenance import SourceInfo, register_source

GP_URL = "https://celestrak.org/NORAD/elements/gp.php"  # canonical; never a redirecting variant
MIN_REFRESH = timedelta(hours=2)  # CelesTrak publishes new GP data every 2 h; never ask sooner

register_source(
    SourceInfo(
        "celestrak",
        "CelesTrak GP orbital elements (TLE)",
        "orbits",
        12 * 60,
        "https://celestrak.org/NORAD/documentation/gp-data-formats.php",
    )
)


@dataclass(frozen=True)
class Satellite:
    """A satellite we predict passes for. `object_name` is what CelesTrak calls it: every fetch
    checks the answer against it, so a mistyped catalogue number can never slip in."""

    key: str  # short id for URLs and CSS classes
    name: str  # the name used across the API, e.g. fire_detection.satellite
    norad_id: int  # NORAD catalogue number, confirmed on CelesTrak 2026-10-30
    object_name: str  # CelesTrak OBJECT_NAME starts with this
    instrument: str  # VIIRS / MODIS / MSI (see analytics.passes.INSTRUMENTS)


# NORAD ids confirmed with CelesTrak `?NAME=...&FORMAT=JSON` (docs/research/firms-and-orbits.md
# section 3.1): looking up by name is unsafe (NAME=TERRA also returns TerraSAR-X and SkyTerra-1).
SATELLITES: tuple[Satellite, ...] = (
    Satellite("snpp", "Suomi NPP", 37849, "SUOMI NPP", "VIIRS"),
    Satellite("noaa20", "NOAA-20", 43013, "NOAA 20", "VIIRS"),
    Satellite("noaa21", "NOAA-21", 54234, "NOAA 21", "VIIRS"),
    Satellite("aqua", "Aqua", 27424, "AQUA", "MODIS"),
    Satellite("terra", "Terra", 25994, "TERRA", "MODIS"),
    Satellite("s2a", "Sentinel-2A", 40697, "SENTINEL-2A", "MSI"),
    Satellite("s2b", "Sentinel-2B", 42063, "SENTINEL-2B", "MSI"),
    Satellite("s2c", "Sentinel-2C", 60989, "SENTINEL-2C", "MSI"),
)
SATELLITE_BY_NAME = {s.name: s for s in SATELLITES}
SATELLITE_BY_KEY = {s.key: s for s in SATELLITES}


class TleFormatError(ValueError):
    """CelesTrak answered 200, but not with the element set we asked for."""


@dataclass(frozen=True)
class TleRecord:
    norad_id: int
    object_name: str
    line1: str
    line2: str
    epoch: datetime  # UTC


def tle_checksum(line: str) -> int:
    """TLE checksum: the sum of the digits in the first 68 columns, plus 1 per minus sign, mod 10."""
    return sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68]) % 10


def _epoch(line1: str) -> datetime:
    """TLE epoch from line 1, columns 19-32: two-digit year (57-99 = 1900s) + day of year."""
    yy = int(line1[18:20])
    year = 1900 + yy if yy >= 57 else 2000 + yy
    return datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=float(line1[20:32]) - 1)


def parse_tle(text: str) -> list[TleRecord]:
    """Parse CelesTrak's 3-line format (name, line 1, line 2), checking each line's checksum."""
    lines = [line.rstrip() for line in text.replace("\r", "").split("\n") if line.strip()]
    records = []
    i = 0
    while i < len(lines):
        named = not lines[i].startswith("1 ")  # a name line precedes line 1 in the 3-line format
        needed = 3 if named else 2
        if i + needed > len(lines):
            raise TleFormatError(f"incomplete element set near {lines[i][:40]!r}")
        if named:
            name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        else:
            name, l1, l2 = "", lines[i], lines[i + 1]
        i += needed
        if not (l1.startswith("1 ") and l2.startswith("2 ")) or len(l1) != 69 or len(l2) != 69:
            raise TleFormatError(f"not a two-line element set: {name!r} {l1[:20]!r}")
        for line in (l1, l2):
            if int(line[68]) != tle_checksum(line):
                raise TleFormatError(f"checksum failed for {name or l1[2:7]}: {line[:20]}...")
        if l1[2:7] != l2[2:7]:
            raise TleFormatError(f"line 1 and line 2 are for different objects ({l1[2:7]}, {l2[2:7]})")
        records.append(TleRecord(int(l1[2:7]), name, l1, l2, _epoch(l1)))
    return records


def fetch_tle(sat: Satellite) -> tuple[TleRecord, FetchResult]:
    """Download one satellite's current elements and check they are for that satellite.

    Raises FetchError when CelesTrak does not answer 200 (the job must then stop calling it) and
    TleFormatError when the answer is not that satellite's TLE ("No GP data found" is a 200).
    """
    result = fetch(
        "celestrak",
        GP_URL,
        params={"CATNR": sat.norad_id, "FORMAT": "TLE"},
        expect="text",
        meta={"satellite": sat.name, "norad_id": sat.norad_id},
    )
    text = result.data or ""
    try:
        records = parse_tle(text)
    except (TleFormatError, ValueError, IndexError) as exc:
        raise TleFormatError(f"{sat.name}: {exc}; answer was {' '.join(text.split())[:120]!r}") from exc
    if len(records) != 1:
        raise TleFormatError(f"{sat.name}: expected 1 element set, got {len(records)}")
    record = records[0]
    if record.norad_id != sat.norad_id:
        raise TleFormatError(f"{sat.name}: asked for {sat.norad_id}, got {record.norad_id}")
    if not record.object_name.upper().startswith(sat.object_name):
        raise TleFormatError(
            f"{sat.name}: NORAD {sat.norad_id} is now called {record.object_name!r}, "
            f"expected {sat.object_name!r}; check the catalogue"
        )
    return record, result


def orbit_mean_elements(line1: str, line2: str, object_name: str) -> dict[str, Any]:
    """The same elements as a CCSDS OMM dict (CelesTrak's FORMAT=JSON keys), derived with sgp4's
    exporter so the browser can call satellite.js json2satrec() on it."""
    return exporter.export_omm(Satrec.twoline2rv(line1, line2), object_name)
