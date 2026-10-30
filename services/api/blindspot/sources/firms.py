"""NASA FIRMS active-fire detections: the keyless regional NRT files and the MAP_KEY area API.

Pure fetch + parse, no database writes (jobs/firms.py stores the rows). Both feeds deliver the same
near-real-time (NRT) product with identical values: on 2026-10-30 the area API and the keyless
NOAA-20 file held the same 611 rows over our area, string for string. So one detection always
normalises to the same FireRow, and the table's unique key (source, acq_utc, lat, lon) keeps one copy
whichever feed brought it first.

What FIRMS actually does (measured 2026-10-30, docs/research/firms-and-orbits.md):
- The keyless South Asia CSVs are rewritten every hour, but their ETag (an MD5 of the content) only
  changes when new detections arrive, and FIRMS ignores conditional GETs. So we HEAD first and GET
  only on a new ETag.
- The area API takes DAY_RANGE 1..5 (a 6+ day call answers "Invalid day range. Expects [1..5]."),
  adds an `instrument` column, writes VIIRS confidence as l/n/h, does not zero-pad acq_time ("828"),
  and names MODIS satellites "Aqua"/"Terra" where the files say "A"/"T".
- Each area call costs 2 transactions per day requested (a 5-day call moved the key's counter by
  10, a 2-day call by 4; the status call and rejected calls cost nothing). The limit is 5000 per
  10 minutes.
- acq_date and acq_time are UTC (FIRMS attribute docs; our orbit model agrees to the minute).
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import SecretStr

from blindspot.config import BBox
from blindspot.http import FetchResult, fetch
from blindspot.provenance import SourceInfo, register_source

FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov"
REGION_FILE = "South_Asia"  # the smallest FIRMS regional file that contains Punjab and Haryana
NRT_WINDOWS = ("24h", "48h", "7d")  # the three keyless files per product, smallest first
MAX_DAY_RANGE = 5  # area API limit, from its own error message (2026-10-30)
TRANSACTIONS_PER_DAY = 2  # measured cost of one area-API call per day requested (see above)
AREA_TIMEOUT_S = 120.0  # an area call usually answers in about a second; leave room for slow days

register_source(
    SourceInfo(
        "firms",
        "NASA FIRMS active fires (VIIRS 375 m, MODIS 1 km; NRT)",
        "fires",
        30,
        "https://firms.modaps.eosdis.nasa.gov/api/",
    )
)


@dataclass(frozen=True)
class Product:
    """One FIRMS NRT product, named as the area API names it."""

    source: str  # stored in fire_detection.source, e.g. VIIRS_NOAA20_NRT
    instrument: str  # VIIRS or MODIS
    folder: str  # keyless file folder under /data/active_fire/
    prefix: str  # keyless file name prefix

    def file_url(self, window: str) -> str:
        """The keyless regional CSV for `window` (24h, 48h or 7d)."""
        if window not in NRT_WINDOWS:
            raise ValueError(f"window must be one of {NRT_WINDOWS}, not {window!r}")
        return (
            f"{FIRMS_BASE}/data/active_fire/{self.folder}/csv/"
            f"{self.prefix}_{REGION_FILE}_{window}.csv"
        )


PRODUCTS: tuple[Product, ...] = (
    Product("VIIRS_NOAA20_NRT", "VIIRS", "noaa-20-viirs-c2", "J1_VIIRS_C2"),
    Product("VIIRS_NOAA21_NRT", "VIIRS", "noaa-21-viirs-c2", "J2_VIIRS_C2"),
    Product("VIIRS_SNPP_NRT", "VIIRS", "suomi-npp-viirs-c2", "SUOMI_VIIRS_C2"),
    Product("MODIS_NRT", "MODIS", "modis-c6.1", "MODIS_C6_1"),
)
PRODUCT_BY_SOURCE = {p.source: p for p in PRODUCTS}

# FIRMS `satellite` codes -> the satellite names used across BlindSpot (fire_detection.satellite,
# /api/orbits). Files write N, N20, N21, A, T; the area API writes Aqua and Terra in full.
# Unknown codes are skipped and counted, never guessed.
SATELLITE_NAMES = {
    "N": "Suomi NPP",
    "N20": "NOAA-20",
    "N21": "NOAA-21",
    "A": "Aqua",
    "AQUA": "Aqua",
    "T": "Terra",
    "TERRA": "Terra",
}
FIRE_SATELLITES = ("Suomi NPP", "NOAA-20", "NOAA-21", "Aqua", "Terra")

VIIRS_CONFIDENCE = {"l": "low", "n": "nominal", "h": "high"}  # area API letters; files use words
# MODIS confidence is a percentage. Classes per the MODIS C6/C6.1 Active Fire User's Guide v1.0
# (Giglio et al., Table 11, as used by FIRMS): low 0-29, nominal 30-79, high 80-100.
MODIS_NOMINAL_FROM_PCT = 30
MODIS_HIGH_FROM_PCT = 80
CONFIDENCE_ORDER = ("low", "nominal", "high")


@dataclass(frozen=True)
class FireRow:
    """One FIRMS detection, normalised so VIIRS and MODIS rows, from files or the API, line up."""

    source: str
    satellite: str
    instrument: str
    acq_utc: datetime
    lat: float
    lon: float
    confidence: str  # low / nominal / high
    confidence_pct: int | None  # MODIS only: the raw 0-100 value
    frp_mw: float | None  # fire radiative power, MW
    bright_ti4: float | None  # K: VIIRS I4 (3.7 um) or MODIS band 21/22 (4 um)
    bright_ti5: float | None  # K: VIIRS I5 (11.45 um) or MODIS band 31 (11 um)
    scan: float | None  # pixel size across track, km (grows towards the swath edge)
    track: float | None  # pixel size along track, km
    daynight: str  # D or N, as FIRMS flags it
    version: str | None  # e.g. 2.0NRT, 6.1NRT

    def as_db_row(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "satellite": self.satellite,
            "instrument": self.instrument,
            "acq_utc": self.acq_utc,
            "lat": self.lat,
            "lon": self.lon,
            "confidence": self.confidence,
            "confidence_pct": self.confidence_pct,
            "frp_mw": self.frp_mw,
            "bright_ti4": self.bright_ti4,
            "bright_ti5": self.bright_ti5,
            "scan": self.scan,
            "track": self.track,
            "daynight": self.daynight,
            "version": self.version,
        }


@dataclass
class ParsedCsv:
    rows: list[FireRow]
    skipped: Counter[str]  # reason -> rows skipped for it
    newest_by_satellite: dict[str, datetime]  # newest acquisition per satellite in the whole file


class FirmsFormatError(ValueError):
    """FIRMS answered, but not with the CSV we expect (an error text or a changed header)."""


REQUIRED_COLUMNS = {
    "latitude", "longitude", "acq_date", "acq_time", "satellite", "confidence", "daynight",
}  # fmt: skip


def _float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def _confidence(raw: str, instrument: str) -> tuple[str, int | None]:
    raw = raw.strip()
    if instrument == "MODIS":
        pct = int(float(raw))
        if not 0 <= pct <= 100:
            raise ValueError(f"MODIS confidence {pct} outside 0-100")
        if pct >= MODIS_HIGH_FROM_PCT:
            return "high", pct
        return ("nominal" if pct >= MODIS_NOMINAL_FROM_PCT else "low"), pct
    word = VIIRS_CONFIDENCE.get(raw.lower(), raw.lower())
    if word not in CONFIDENCE_ORDER:
        raise ValueError(f"unknown VIIRS confidence {raw!r}")
    return word, None


def parse_csv(text: str, product: Product) -> ParsedCsv:
    """Parse a FIRMS CSV (keyless file or area API) for `product` into FireRows.

    Columns are read by name, so the area API's extra `instrument` column and MODIS's
    `brightness`/`bright_t31` names are handled. A body that is not this CSV (FIRMS sometimes
    answers 200 with an error sentence) raises FirmsFormatError; a single bad row is skipped and
    counted in `skipped`, never repaired by guessing.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = {name.strip() for name in reader.fieldnames or []}
    missing = REQUIRED_COLUMNS - header
    if missing:
        snippet = " ".join(text.split())[:160]
        raise FirmsFormatError(f"not a FIRMS CSV (missing {sorted(missing)}): {snippet!r}")
    b4, b5 = ("brightness", "bright_t31") if product.instrument == "MODIS" else (
        "bright_ti4", "bright_ti5")  # fmt: skip

    rows: list[FireRow] = []
    skipped: Counter[str] = Counter()
    newest: dict[str, datetime] = {}
    for record in reader:
        try:
            code = (record.get("satellite") or "").strip()
            satellite = SATELLITE_NAMES.get(code.upper()) or SATELLITE_NAMES.get(code)
            if satellite is None:
                skipped[f"unknown satellite code {code!r}"] += 1
                continue
            instrument = (record.get("instrument") or product.instrument).strip().upper()
            if instrument != product.instrument:
                skipped[f"instrument {instrument} in a {product.source} file"] += 1
                continue
            hhmm = (record.get("acq_time") or "").strip().zfill(4)
            acq_utc = datetime.strptime(f"{record['acq_date'].strip()} {hhmm}", "%Y-%m-%d %H%M")
            confidence, pct = _confidence(record.get("confidence") or "", instrument)
            daynight = (record.get("daynight") or "").strip().upper()
            if daynight not in ("D", "N"):
                skipped[f"daynight {daynight!r}"] += 1
                continue
            row = FireRow(
                source=product.source,
                satellite=satellite,
                instrument=instrument,
                acq_utc=acq_utc.replace(tzinfo=UTC),
                lat=float(record["latitude"]),
                lon=float(record["longitude"]),
                confidence=confidence,
                confidence_pct=pct,
                frp_mw=_float(record.get("frp")),
                bright_ti4=_float(record.get(b4)),
                bright_ti5=_float(record.get(b5)),
                scan=_float(record.get("scan")),
                track=_float(record.get("track")),
                daynight=daynight,
                version=(record.get("version") or "").strip() or None,
            )
        except (KeyError, ValueError, TypeError) as exc:
            skipped[f"unparseable row ({type(exc).__name__})"] += 1
            continue
        if not (-90 <= row.lat <= 90 and -180 <= row.lon <= 180):
            skipped["coordinates out of range"] += 1
            continue
        rows.append(row)
        if row.acq_utc > newest.get(satellite, datetime.min.replace(tzinfo=UTC)):
            newest[satellite] = row.acq_utc
    return ParsedCsv(rows, skipped, newest)


def clip_to_bbox(rows: list[FireRow], bbox: BBox) -> list[FireRow]:
    """Rows inside bbox. Only a pre-filter for storage: counts are clipped to Indian state
    polygons later, because this box also holds part of Pakistan's Punjab."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return [r for r in rows if min_lon <= r.lon <= max_lon and min_lat <= r.lat <= max_lat]


# --- keyless files -------------------------------------------------------------------------------


def head_file(product: Product, window: str) -> FetchResult:
    """HEAD a keyless file: its ETag tells whether the content changed since our last download."""
    return fetch(
        "firms",
        product.file_url(window),
        method="HEAD",
        expect="text",
        meta={"product": product.source, "window": window, "check": "etag"},
    )


def get_file(product: Product, window: str) -> FetchResult:
    """Download a keyless file (CSV text in result.data, ETag in result.headers)."""
    return fetch(
        "firms",
        product.file_url(window),
        expect="text",
        meta={"product": product.source, "window": window},
    )


# --- area API (MAP_KEY) --------------------------------------------------------------------------


def area_url(key: SecretStr, source: str, bbox: BBox, day_range: int, start: date) -> str:
    """The area-API URL. The key sits in the path; fetch() masks it in every log and record."""
    if not 1 <= day_range <= MAX_DAY_RANGE:
        raise ValueError(f"day_range must be 1..{MAX_DAY_RANGE}, not {day_range}")
    area = ",".join(f"{v:g}" for v in bbox)
    return (
        f"{FIRMS_BASE}/api/area/csv/{key.get_secret_value()}/{source}/{area}/"
        f"{day_range}/{start.isoformat()}"
    )


def get_area(key: SecretStr, source: str, bbox: BBox, day_range: int, start: date) -> FetchResult:
    """Detections of `source` inside bbox for `day_range` UTC days from `start` (area API)."""
    return fetch(
        "firms",
        area_url(key, source, bbox, day_range, start),
        expect="text",
        timeout=AREA_TIMEOUT_S,
        meta={
            "product": source,
            "start": start.isoformat(),
            "day_range": day_range,
            "transactions_est": day_range * TRANSACTIONS_PER_DAY,
        },
    )


@dataclass(frozen=True)
class KeyStatus:
    limit: int
    used: int
    interval: str  # "10 minutes"


def key_status(key: SecretStr) -> KeyStatus:
    """How many transactions the key has used in the current interval (the call itself is free)."""
    result = fetch(
        "firms",
        f"{FIRMS_BASE}/mapserver/mapkey_status/",
        params={"MAP_KEY": key.get_secret_value()},
        meta={"check": "mapkey_status"},
    )
    body = result.data or {}
    try:
        return KeyStatus(
            int(body["transaction_limit"]),
            int(body["current_transactions"]),
            str(body.get("transaction_interval") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FirmsFormatError(f"unexpected mapkey_status answer: {str(body)[:160]!r}") from exc


def data_availability(key: SecretStr) -> dict[str, tuple[date, date]]:
    """First and last available UTC day of every FIRMS source, e.g. {'MODIS_NRT': (09-01, 10-30)}."""
    result = fetch(
        "firms",
        f"{FIRMS_BASE}/api/data_availability/csv/{key.get_secret_value()}/ALL",
        expect="text",
        meta={"check": "data_availability"},
    )
    reader = csv.DictReader(io.StringIO(result.data or ""))
    if not {"data_id", "min_date", "max_date"} <= set(reader.fieldnames or []):
        raise FirmsFormatError(f"unexpected data_availability answer: {result.data[:160]!r}")
    return {
        r["data_id"]: (date.fromisoformat(r["min_date"]), date.fromisoformat(r["max_date"]))
        for r in reader
    }
