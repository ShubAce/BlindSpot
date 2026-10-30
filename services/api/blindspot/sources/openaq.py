"""OpenAQ v3: the CPCB-network monitoring stations (OpenAQ provider 168) and their PM2.5 / PM10.

Why OpenAQ: CPCB's own portal is not a public API, and the data.gov.in feed carries AQI
sub-indices, not concentrations. OpenAQ republishes the CPCB stations' 15-minute readings in µg/m³,
with history, behind a free key sent in the X-API-Key header.

Calls used (all verified live on 2026-10-30, see tests/fixtures/air/):
- /v3/locations?bbox=...&providers_id=168     every CPCB station in a box, with its sensors;
- /v3/locations/{id}/latest                   the newest value of each sensor of one station;
- /v3/sensors/{id}/measurements/hourly        hourly means of one sensor (its history).

Rate limit: 60 requests a minute per key (docs.openaq.org; every response carries
x-ratelimit-remaining and x-ratelimit-reset). `OpenAQClient` waits for the window to reset before
it would run out, instead of collecting 429s. Pure fetch + parse: the air job decides what to store.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from blindspot.config import BBox
from blindspot.http import FetchError, FetchResult, fetch, set_fetch_records

log = logging.getLogger(__name__)

BASE_URL = "https://api.openaq.org/v3"
SOURCE_KEY = "openaq"
CPCB_PROVIDER_ID = 168  # OpenAQ's id for "CPCB" (explore.openaq.org/locations/50: provider CPCB)
PARAMETERS = ("pm25", "pm10")
# CPCB stations report 15-minute averages: /measurements of a CPCB sensor gives period.interval
# "00:15:00" (verified for sensor 12234787, R K Puram, on 2026-10-30).
LATEST_PERIOD_MINUTES = 15
# An hourly mean counts only when at least 75 % of its 15-minute readings exist (3 of 4): the same
# data-capture rule this module applies to 24-hour means (routers/air.py, MIN_HOURS_FOR_DAILY_MEAN).
# OpenAQ averages whatever readings an hour has, so without this a single reading (25 %) would be
# served as that hour's mean, and so would the newest hour while its readings are still arriving.
MIN_HOURLY_PERCENT_COMPLETE = 75.0
RATE_LIMIT_WAIT_S = 61.0  # a full rate-limit window, waited after an unexpected 429


@dataclass(frozen=True)
class Location:
    """A CPCB station as OpenAQ lists it, with its PM sensors ({sensor id: "pm25" | "pm10"})."""

    id: int
    name: str
    lat: float
    lon: float
    last_utc: datetime | None  # when any of its sensors last reported
    sensors: Mapping[int, str]


@dataclass(frozen=True)
class Reading:
    """One PM value of one sensor. observed_at is the end of its averaging period, in UTC."""

    location_id: int
    sensor_id: int
    parameter: str
    value: float
    observed_at: datetime
    period_minutes: int


def _utc(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _valid(value: Any) -> float | None:
    """A usable concentration: a finite number that is not negative (CPCB feeds occasionally
    carry negative or missing values from instrument faults; we drop those, never repair them)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


# --- parsing (pure functions, tested on recorded responses) -------------------------------------


def parse_locations(payload: Mapping[str, Any]) -> list[Location]:
    """Locations with their PM2.5/PM10 sensors in µg/m³ (other sensors are ignored)."""
    out = []
    for item in payload.get("results") or []:
        coords = item.get("coordinates") or {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if lat is None or lon is None:
            continue
        sensors = {
            int(s["id"]): s["parameter"]["name"]
            for s in item.get("sensors") or []
            if s.get("parameter", {}).get("name") in PARAMETERS
            and s["parameter"].get("units") == "µg/m³"
        }
        last = (item.get("datetimeLast") or {}).get("utc")
        out.append(
            Location(
                int(item["id"]),
                str(item.get("name") or ""),
                float(lat),
                float(lon),
                _utc(last),
                sensors,
            )
        )
    return out


def parse_latest(payload: Mapping[str, Any], location: Location) -> list[Reading]:
    """The newest PM2.5 and PM10 reading of a location.

    A station can carry two sensors for one parameter: a legacy one that stopped years ago and the
    current one (R K Puram has pm25 sensors 35, last 2018, and 12234787, live). Keep the newest.
    """
    best: dict[str, Reading] = {}
    for item in payload.get("results") or []:
        sensor_id = int(item.get("sensorsId") or 0)
        parameter = location.sensors.get(sensor_id)
        value = _valid(item.get("value"))
        when = _utc((item.get("datetime") or {}).get("utc"))
        if parameter is None or value is None or when is None:
            continue
        reading = Reading(location.id, sensor_id, parameter, value, when, LATEST_PERIOD_MINUTES)
        if parameter not in best or when > best[parameter].observed_at:
            best[parameter] = reading
    return [best[p] for p in PARAMETERS if p in best]


def parse_hourly(
    payload: Mapping[str, Any], *, location_id: int, sensor_id: int, parameter: str
) -> list[Reading]:
    """Hourly means of one sensor. Hours that OpenAQ flags as suspect, and hours with fewer than
    75 % of their readings (coverage.percentComplete), are skipped. An incomplete newest hour is
    picked up by a later run: the air job re-reads the last hours once newer readings exist."""
    out = []
    for item in payload.get("results") or []:
        if (item.get("flagInfo") or {}).get("hasFlags"):
            continue
        complete = (item.get("coverage") or {}).get("percentComplete")
        if isinstance(complete, (int, float)) and complete < MIN_HOURLY_PERCENT_COMPLETE:
            continue
        period = item.get("period") or {}
        end = _utc((period.get("datetimeTo") or {}).get("utc"))
        value = _valid(item.get("value"))
        if end is None or value is None or period.get("interval") != "01:00:00":
            continue
        out.append(Reading(location_id, sensor_id, parameter, value, end, 60))
    return out


# --- calls ---------------------------------------------------------------------------------------


class _RateGate:
    """Spends OpenAQ's per-minute budget without running into 429s.

    Each response says how many requests are left in the window (x-ratelimit-remaining) and how
    long until it resets (x-ratelimit-reset, seconds). When only `reserve` are left, the next call
    first sleeps until the reset. Shared by every client in the process: the budget is per key.
    """

    def __init__(self, reserve: int = 2) -> None:
        self.reserve = reserve
        self.remaining: int | None = None
        self.reset_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            if self.remaining is not None and self.remaining <= self.reserve:
                pause = self.reset_at - time.monotonic()
                if pause > 0:
                    log.info("openaq: rate-limit window used up; waiting %.0f s", pause)
                    time.sleep(min(pause, RATE_LIMIT_WAIT_S))
                self.remaining = None

    def update(self, headers: Mapping[str, str]) -> None:
        try:
            remaining = int(headers["x-ratelimit-remaining"])
            reset_s = float(headers["x-ratelimit-reset"])
        except (KeyError, TypeError, ValueError):
            return
        with self.lock:
            self.remaining = remaining
            self.reset_at = time.monotonic() + max(reset_s, 0.0)

    def exhausted(self) -> None:
        """A 429 arrived anyway (another process shares the key): wait a whole window next time."""
        with self.lock:
            self.remaining = 0
            self.reset_at = time.monotonic() + RATE_LIMIT_WAIT_S


_GATE = _RateGate()


class OpenAQClient:
    """OpenAQ v3 calls through blindspot.http.fetch (logged in source_fetch, key masked)."""

    def __init__(self, api_key: str) -> None:
        self._headers = {"X-API-Key": api_key, "Accept": "application/json"}
        self.calls = 0

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> FetchResult:
        """GET one endpoint, paced by the shared rate gate; one retry after a 429."""
        for attempt in (1, 2):
            _GATE.wait()
            self.calls += 1
            try:
                result = fetch(
                    SOURCE_KEY, f"{BASE_URL}{path}", params=params, headers=self._headers
                )
            except FetchError as exc:
                if exc.status_code == 429 and attempt == 1:
                    _GATE.exhausted()
                    continue
                raise
            _GATE.update(result.headers)
            return result
        raise AssertionError("unreachable")  # the loop returns or raises

    def locations(self, bbox: BBox, provider_id: int = CPCB_PROVIDER_ID) -> list[Location]:
        """Every location of a provider inside bbox (paged, 1000 per page)."""
        out: list[Location] = []
        page = 1
        while True:
            params = {
                "bbox": ",".join(f"{v:g}" for v in bbox),
                "providers_id": provider_id,
                "limit": 1000,
                "page": page,
            }
            result = self.get("/locations", params)
            batch = parse_locations(result.data or {})
            set_fetch_records(result.fetch_id, len(batch))
            out += batch
            found = (result.data or {}).get("meta", {}).get("found")
            if len(batch) < 1000 or not isinstance(found, int) or len(out) >= found:
                return out
            page += 1

    def latest(self, location: Location) -> tuple[list[Reading], int | None]:
        """The newest PM2.5/PM10 of one location, and the fetch it came from."""
        result = self.get(f"/locations/{location.id}/latest", {"limit": 1000})
        readings = parse_latest(result.data or {}, location)
        set_fetch_records(result.fetch_id, len(readings))
        return readings, result.fetch_id

    def hourly(
        self,
        *,
        location_id: int,
        sensor_id: int,
        parameter: str,
        since: datetime,
        until: datetime,
    ) -> tuple[list[Reading], int | None]:
        """Hourly means of one sensor between two times (at most 1000 hours per call)."""
        params = {
            "datetime_from": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datetime_to": until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 1000,
        }
        result = self.get(f"/sensors/{sensor_id}/measurements/hourly", params)
        readings = parse_hourly(
            result.data or {}, location_id=location_id, sensor_id=sensor_id, parameter=parameter
        )
        set_fetch_records(result.fetch_id, len(readings))
        return readings, result.fetch_id
