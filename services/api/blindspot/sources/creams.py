"""CREAMS (ICAR-IARI) official residue-burning events: the count India's government reports.

CREAMS publishes one GeoServer WFS layer per day of the season, `geokrishi_v:{d}_{month}_{yyyy}_rice`
(day not zero-padded, English month in lower case, e.g. `29_october_2026_rice`). Keyless, small
(5-300 points a day) and fast (0.05-0.3 s). Measured 2026-10-30:
- properties: satellite (S-NPP, AQUA), latitude, longitude, acq_date (DDMMYYYY as a number, no zero
  padding: 1102026 is 1 Oct), acq_time (HH:MM:SS, IST), day_flag, fire_power (MW), district, block;
  no state field.
- a layer that does not exist (yet) answers HTTP 400 with an OGC ExceptionReport ("Feature type
  ... unknown"). Some days never get a rice layer (2026-09-17 has only the all-India `al_ind`
  layer), so the job first reads the workspace's capabilities (one 0.3 s call) and asks only for
  layers that exist, instead of collecting an expected 400 every hour.
- satellites seen in the 2026 season layers: S-NPP, AQUA, JPSS-1 (= NOAA-20) and TERRA.

Pure fetch + parse; jobs/firms.py stores the events. Pitch wording per SYNTHESIS: "the official
layer contained only S-NPP and Aqua events on the days checked". CREAMS data is "not for legal
purposes" (its own notice); credit "Official counts: CREAMS, ICAR-IARI".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from blindspot.http import FetchError, FetchResult, fetch
from blindspot.provenance import SourceInfo, register_source

WFS_URL = "https://creams.iari.res.in/geokrishi/geoserver/wfs"
CAPABILITIES_URL = "https://creams.iari.res.in/geokrishi/geoserver/geokrishi_v/wfs"  # workspace only
LAYER = "rice"  # the paddy (kharif) residue layer; the season of this product
IST_OFFSET = timedelta(hours=5, minutes=30)  # CREAMS times are Indian Standard Time
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip  (fixed English names: strftime('%B') depends on the machine's locale)

# CREAMS satellite names -> the names used across BlindSpot. Unknown names are kept as given.
SATELLITE_NAMES = {
    "S-NPP": "Suomi NPP",
    "SNPP": "Suomi NPP",
    "SUOMI NPP": "Suomi NPP",
    "NOAA-20": "NOAA-20",
    "NOAA20": "NOAA-20",
    "JPSS-1": "NOAA-20",  # NOAA-20 was launched as JPSS-1; CREAMS still uses that name
    "NOAA-21": "NOAA-21",
    "NOAA21": "NOAA-21",
    "JPSS-2": "NOAA-21",
    "AQUA": "Aqua",
    "TERRA": "Terra",
}

register_source(
    SourceInfo(
        "creams",
        "CREAMS, ICAR-IARI: official residue-burning events",
        "fires_official",
        60,
        "https://creams.iari.res.in/",
    )
)


class LayerNotPublished(LookupError):
    """CREAMS has no layer for that day (not published yet, or never)."""


class CreamsFormatError(ValueError):
    """CREAMS answered, but not with the GeoJSON layer we expect."""


@dataclass(frozen=True)
class OfficialEvent:
    layer_date: date  # the IST day of the layer
    satellite: str  # our name (Suomi NPP, Aqua, ...), or CREAMS's own when unknown
    satellite_raw: str
    acq_utc: datetime
    lat: float
    lon: float
    frp_mw: float | None
    daynight: str | None
    creams_district: str | None
    creams_block: str | None

    def as_db_row(self) -> dict[str, Any]:
        return {
            "layer": LAYER,
            "layer_date": self.layer_date,
            "satellite": self.satellite,
            "satellite_raw": self.satellite_raw,
            "acq_utc": self.acq_utc,
            "lat": self.lat,
            "lon": self.lon,
            "frp_mw": self.frp_mw,
            "daynight": self.daynight,
            "creams_district": self.creams_district,
            "creams_block": self.creams_block,
        }


def layer_name(day: date, layer: str = LAYER) -> str:
    """WFS type name of a day's layer, e.g. geokrishi_v:1_october_2026_rice."""
    return f"geokrishi_v:{day.day}_{_MONTHS[day.month - 1]}_{day.year}_{layer}"


_LAYER_RE = re.compile(r"geokrishi_v:(\d{1,2})_([a-z]+)_(\d{4})_([a-z_]+)")


def available_layers(layer: str = LAYER) -> tuple[set[date], FetchResult]:
    """The days for which CREAMS publishes a `layer` layer, from the WFS capabilities document."""
    result = fetch(
        "creams",
        CAPABILITIES_URL,
        params={"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"},
        expect="text",
        meta={"check": "capabilities"},
    )
    days: set[date] = set()
    for d, month, year, kind in _LAYER_RE.findall(result.data or ""):
        if kind == layer and month in _MONTHS:
            try:
                days.add(date(int(year), _MONTHS.index(month) + 1, int(d)))
            except ValueError:  # e.g. a mistyped 31_september layer: not a real day
                continue
    if not days and "FeatureTypeList" not in (result.data or ""):
        raise CreamsFormatError("CREAMS capabilities document has no feature type list")
    return days, result


def _acq_date(value: Any) -> date:
    """CREAMS acq_date: DDMMYYYY as a number without zero padding (1102026 = 1 Oct 2026)."""
    digits = str(int(float(value))).zfill(8)
    return date(int(digits[4:]), int(digits[2:4]), int(digits[:2]))


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def parse_layer(body: Any, day: date) -> tuple[list[OfficialEvent], int]:
    """GeoJSON FeatureCollection -> events, plus how many features were skipped as unreadable.

    The event time is the layer's IST date and acq_time, converted to UTC. A feature whose
    acq_date disagrees with the layer is skipped and counted, not moved to another day.
    """
    if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
        raise CreamsFormatError(f"not a GeoJSON FeatureCollection: {str(body)[:160]!r}")
    events: list[OfficialEvent] = []
    skipped = 0
    for feature in body.get("features") or []:
        props = feature.get("properties") or {}
        try:
            lon, lat = (float(c) for c in (feature.get("geometry") or {})["coordinates"][:2])
            if _acq_date(props["acq_date"]) != day:
                skipped += 1
                continue
            local = datetime.combine(day, time.fromisoformat(str(props["acq_time"]).strip()))
            raw = str(props.get("satellite") or "").strip()
            power = props.get("fire_power")
            flag = _clean(props.get("day_flag"))
            events.append(
                OfficialEvent(
                    layer_date=day,
                    satellite=SATELLITE_NAMES.get(raw.upper(), raw),
                    satellite_raw=raw,
                    acq_utc=(local - IST_OFFSET).replace(tzinfo=UTC),
                    lat=lat,
                    lon=lon,
                    frp_mw=float(power) if power not in (None, "") else None,
                    daynight=flag.upper()[:1] if flag else None,
                    creams_district=_clean(props.get("district")),
                    creams_block=_clean(props.get("block")),
                )
            )
        except (KeyError, TypeError, ValueError):
            skipped += 1
    return events, skipped


def fetch_layer(day: date) -> tuple[list[OfficialEvent], int, FetchResult]:
    """One day's official events. Raises LayerNotPublished when CREAMS has no layer for it."""
    try:
        result = fetch(
            "creams",
            WFS_URL,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": layer_name(day),
                "outputFormat": "application/json",
            },
            meta={"layer": LAYER, "day": day.isoformat()},
        )
    except FetchError as exc:
        # The recorded error text is cut short, so match the report's opening tag, not its code.
        if exc.status_code == 400 and "ExceptionReport" in str(exc):
            raise LayerNotPublished(f"CREAMS has no {LAYER} layer for {day}") from exc
        raise
    events, skipped = parse_layer(result.data, day)
    return events, skipped, result
