"""CPCB's real-time air-quality feed on data.gov.in, and how to read its numbers (NAQI).

Resource 3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69, "Real time Air Quality Index from various locations",
published by CPCB under the Government Open Data License - India. One row per station x pollutant,
refreshed hourly (about HH:33 IST). A personal key (DATA_GOV_IN_API_KEY) allows 1000 rows a page.

The trap: `avg_value` is the pollutant's NAQI **sub-index** (0-500), not a concentration. Verified
by cross-checking against CPCB's own AQI bulletin (docs/research/air-quality-ground.md §2.3:
Amritsar PM2.5 167 here vs bulletin AQI 178; read as µg/m³ it would be ~336). So this module
returns sub-indices, the air job stores them as `<pollutant>_subindex`, and the station AQI is
computed the way CPCB does it (station_aqi below). `last_update` is the batch time in IST, the end
of CPCB's rolling averaging window, identical for every row of a batch.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from blindspot.config import BBox
from blindspot.geo import IST, in_bbox
from blindspot.http import fetch, set_fetch_records

log = logging.getLogger(__name__)

RESOURCE_URL = "https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
SOURCE_KEY = "datagov_aqi"
PAGE_SIZE = 1000
MAX_RESTARTS = 2

# Feed pollutant id -> our parameter stem.
POLLUTANTS = {
    "PM2.5": "pm25",
    "PM10": "pm10",
    "NO2": "no2",
    "SO2": "so2",
    "CO": "co",
    "OZONE": "o3",
    "NH3": "nh3",
}
# NAQI averaging windows (CPCB, "National Air Quality Index", report CUPS/82/2014-15, Table 3.11):
# 24 h for PM2.5, PM10, NO2, SO2 and NH3; 8 h for CO and O3.
WINDOW_MINUTES = {
    "pm25": 1440,
    "pm10": 1440,
    "no2": 1440,
    "so2": 1440,
    "nh3": 1440,
    "co": 480,
    "o3": 480,
}

# NAQI categories by index (same CPCB report): upper bound of each band.
CATEGORIES = (
    (50, "Good"),
    (100, "Satisfactory"),
    (200, "Moderate"),
    (300, "Poor"),
    (400, "Very Poor"),
    (500, "Severe"),
)

# NAQI breakpoints (same CPCB report, Table 3.11): (C_lo, C_hi, I_lo, I_hi), 24-h µg/m³. The
# Severe band (index 401-500) is open-ended (PM2.5 "250+", PM10 "430+"), so it is not inverted.
BREAKPOINTS = {
    "pm25": (
        (0, 30, 0, 50),
        (31, 60, 51, 100),
        (61, 90, 101, 200),
        (91, 120, 201, 300),
        (121, 250, 301, 400),
    ),
    "pm10": (
        (0, 50, 0, 50),
        (51, 100, 51, 100),
        (101, 250, 101, 200),
        (251, 350, 201, 300),
        (351, 430, 301, 400),
    ),
}


@dataclass(frozen=True)
class SubIndex:
    """One station's NAQI sub-index for one pollutant at one batch time."""

    station: str
    city: str | None
    state: str | None
    lat: float
    lon: float
    pollutant: str  # pm25, pm10, no2, so2, co, o3, nh3
    value: int
    observed_at: datetime  # batch time, UTC
    window_minutes: int


@dataclass(frozen=True)
class Batch:
    rows: list[SubIndex]
    fetch_ids: list[int | None]  # one per page, in page order
    row_fetch: list[int | None]  # the fetch each row came from (same order as rows)
    updated_date: str | None  # the feed's own refresh stamp
    total: int  # rows the feed holds (all India)


def _batch_time(text: str) -> datetime:
    """`dd-mm-YYYY HH:MM:SS` in IST -> aware UTC."""
    return datetime.strptime(text, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST).astimezone(UTC)


def parse_records(records: Sequence[Mapping[str, Any]], bbox: BBox | None = None) -> list[SubIndex]:
    """Feed records -> SubIndex rows. "NA" values and rows outside bbox are dropped."""
    out = []
    for r in records:
        pollutant = POLLUTANTS.get(str(r.get("pollutant_id") or ""))
        raw = str(r.get("avg_value") or "").strip()
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
            value = int(float(raw))
            when = _batch_time(str(r["last_update"]))
        except (KeyError, TypeError, ValueError):
            continue  # "NA", or a malformed row: skip it, never guess
        if pollutant is None or value < 0 or (bbox is not None and not in_bbox(lon, lat, bbox)):
            continue
        out.append(
            SubIndex(
                station=" ".join(str(r.get("station") or "").split()),
                city=(" ".join(str(r.get("city") or "").split()) or None),
                state=(" ".join(str(r.get("state") or "").split()) or None),
                lat=lat,
                lon=lon,
                pollutant=pollutant,
                value=value,
                observed_at=when,
                window_minutes=WINDOW_MINUTES[pollutant],
            )
        )
    return out


def _newest_batch(records: Sequence[Mapping[str, Any]]) -> datetime | None:
    times = []
    for r in records:
        try:
            times.append(_batch_time(str(r["last_update"])))
        except (KeyError, ValueError):
            continue
    return max(times, default=None)


def fetch_all(api_key: str, bbox: BBox | None = None) -> Batch:
    """Every row of the feed (all India, paged), keeping those inside bbox.

    CPCB refreshes the feed hourly and rows flip a few seconds before `updated_date` is re-stamped,
    so a multi-page pull can straddle a refresh. If either the stamp or the newest batch time
    changes between pages, start again (at most MAX_RESTARTS times): one consistent batch.
    """
    params: dict[str, Any] = {
        "api-key": api_key,
        "format": "json",
        "limit": PAGE_SIZE,
        "sort[station]": "asc",
    }
    for _attempt in range(MAX_RESTARTS + 1):
        rows: list[SubIndex] = []
        row_fetch: list[int | None] = []
        fetch_ids: list[int | None] = []
        stamp: str | None = None
        batch_time: datetime | None = None
        offset, total, restarted = 0, 0, False
        while True:
            result = fetch(SOURCE_KEY, RESOURCE_URL, params={**params, "offset": offset})
            data = result.data or {}
            records = data.get("records") or []
            newest = _newest_batch(records)
            if fetch_ids and (
                data.get("updated_date") != stamp
                or (newest is not None and batch_time is not None and newest > batch_time)
            ):
                restarted = True
                log.info("datagov: feed refreshed during the pull; starting again")
                break
            stamp = data.get("updated_date")
            if newest is not None and (batch_time is None or newest > batch_time):
                batch_time = newest
            page_rows = parse_records(records, bbox)
            set_fetch_records(result.fetch_id, len(page_rows), {"offset": offset})
            fetch_ids.append(result.fetch_id)
            rows += page_rows
            row_fetch += [result.fetch_id] * len(page_rows)
            total = int(data.get("total") or 0)
            offset += int(data.get("count") or len(records) or 0)
            if not records or offset >= total:
                break
        if not restarted:
            return Batch(rows, fetch_ids, row_fetch, stamp, total)
    raise RuntimeError("data.gov.in kept refreshing during the pull; try again later")


def mark_stored(batch: Batch) -> None:
    """Note on the pull's last page that this batch is stored (meta.complete_batch = the feed's
    stamp). The air job skips a pull while the feed's stamp equals the newest one noted, so call
    this only after the rows are committed: the feed keeps no history, and a batch skipped after a
    failed write would be lost for good."""
    if batch.fetch_ids:
        last = batch.fetch_ids[-1]
        rows = sum(1 for f in batch.row_fetch if f == last)
        set_fetch_records(last, rows, {"complete_batch": batch.updated_date})


def feed_stamp(api_key: str) -> str | None:
    """The feed's `updated_date`, from a one-row request (a few hundred bytes instead of 1.7 MB).
    CPCB re-stamps it with every hourly batch, so an unchanged stamp means nothing new to read."""
    result = fetch(
        SOURCE_KEY,
        RESOURCE_URL,
        params={"api-key": api_key, "format": "json", "limit": 1},
        meta={"kind": "stamp"},
    )
    stamp = (result.data or {}).get("updated_date")
    set_fetch_records(result.fetch_id, 0, {"updated_date": stamp})
    return stamp


# --- reading NAQI numbers ------------------------------------------------------------------------


def aqi_category(index: float | None) -> str | None:
    """The NAQI category of an index value (CPCB 2014)."""
    if index is None:
        return None
    for upper, name in CATEGORIES:
        if index <= upper:
            return name
    return "Severe"


def station_aqi(subindex: Mapping[str, float]) -> tuple[int | None, list[str]]:
    """A station's AQI from its pollutant sub-indices, by CPCB's rule: the worst sub-index, and
    only when at least three pollutants are available, one of them PM2.5 or PM10. Returns
    (aqi, prominent pollutants), or (None, []) when the rule's data requirement is not met."""
    if len(subindex) < 3 or not ({"pm25", "pm10"} & set(subindex)):
        return None, []
    worst = max(subindex.values())
    return int(worst), sorted(p for p, v in subindex.items() if v == worst)


def subindex_to_concentration(pollutant: str, index: float) -> float | None:
    """Invert a PM sub-index to the 24-hour mean concentration it stands for (µg/m³), linearly
    within its NAQI band. None for other pollutants and for the open-ended Severe band."""
    for c_lo, c_hi, i_lo, i_hi in BREAKPOINTS.get(pollutant, ()):
        if index <= i_hi:
            if index < i_lo:  # between two integer bands (e.g. 50.5): use the band's floor
                return float(c_lo)
            return round(c_lo + (index - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)
    return None
