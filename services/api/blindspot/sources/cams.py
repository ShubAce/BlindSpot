"""The Copernicus CAMS global air-quality model, through Open-Meteo's keyless air-quality API.

Why a model next to the stations: it is the baseline every forecaster uses, and it is fire-blind
in exactly our way. CAMS takes its fire emissions from GFAS, which uses MODIS and VIIRS
polar-orbiter detections, so it cannot see the ~5 PM burns those satellites miss (docs/research/
meteo-and-trajectories.md §4). Model values are stored and served apart from measurements, always.

Facts verified live on 2026-10-30: PM is native 0.4° (values are constant across 0.4° blocks;
returned cells snap to a 0.1° grid), hourly steps, a new run every 12 h about 9.8 h after its start
time, about 5 days of forecast; `past_days` up to 92 works. meta.json says when a new run lands,
so the air job refetches only then. Attribution: "CAMS via Open-Meteo".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from blindspot.http import FetchResult, fetch, set_fetch_records

AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
META_URL = "https://air-quality-api.open-meteo.com/data/cams_global/static/meta.json"
SOURCE_KEY = "cams"
VARIABLES = {"pm2_5": "pm25", "pm10": "pm10"}  # Open-Meteo name -> our parameter
MAX_LOCATIONS = 1000  # Open-Meteo's cap per request


@dataclass(frozen=True)
class CamsRun:
    """The newest CAMS run Open-Meteo serves."""

    initialised: datetime  # model start time
    available: datetime  # when Open-Meteo had it
    data_end: datetime | None  # last forecast hour


@dataclass(frozen=True)
class Point:
    """Where to sample the model: a name (our city) and a position."""

    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class ModelSeries:
    """Hourly model values at one point. lat/lon are the model cell Open-Meteo actually used."""

    name: str
    lat: float
    lon: float
    times: list[datetime]
    values: dict[str, list[float | None]]  # parameter -> one value per time


def _from_unix(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value is not None else None


def parse_meta(payload: Mapping[str, Any]) -> CamsRun:
    initialised = _from_unix(payload.get("last_run_initialisation_time"))
    available = _from_unix(payload.get("last_run_availability_time"))
    if initialised is None or available is None:
        raise ValueError("CAMS meta.json has no run times")
    return CamsRun(initialised, available, _from_unix(payload.get("data_end_time")))


def latest_run() -> tuple[CamsRun, FetchResult]:
    """The newest CAMS run (a tiny static file; costs nothing against the API quota)."""
    result = fetch(SOURCE_KEY, META_URL, meta={"kind": "meta"})
    run = parse_meta(result.data or {})
    set_fetch_records(result.fetch_id, 1)
    return run, result


def parse_series(payload: Any, points: Sequence[Point]) -> list[ModelSeries]:
    """Open-Meteo answers one location with an object and several with an array in request
    order (the first element has no location_id), so position i belongs to points[i]."""
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(points):
        raise ValueError(f"asked for {len(points)} locations, got {len(items)}")
    out = []
    for point, item in zip(points, items, strict=True):
        hourly = item.get("hourly") or {}
        times = [datetime.fromtimestamp(int(t), UTC) for t in hourly.get("time") or []]
        values = {}
        for name, parameter in VARIABLES.items():
            column = list(hourly.get(name) or [])
            values[parameter] = (column + [None] * len(times))[: len(times)]
        out.append(
            ModelSeries(
                point.name, float(item["latitude"]), float(item["longitude"]), times, values
            )
        )
    return out


def fetch_series(
    points: Sequence[Point],
    *,
    past_days: int = 7,
    forecast_days: int = 5,
    meta: dict[str, Any] | None = None,
) -> tuple[list[ModelSeries], FetchResult]:
    """Hourly PM2.5 and PM10 at each point for past_days back and forecast_days ahead.

    Two variables over 12 days stay under Open-Meteo's "one call per location" weight (≤10
    variables, ≤14 days), so N points cost N calls of the free daily 10,000.
    """
    if not points or len(points) > MAX_LOCATIONS:
        raise ValueError(f"give 1 to {MAX_LOCATIONS} points per request, not {len(points)}")
    params = {
        "latitude": ",".join(f"{p.lat:.4f}" for p in points),
        "longitude": ",".join(f"{p.lon:.4f}" for p in points),
        "hourly": ",".join(VARIABLES),
        "past_days": past_days,
        "forecast_days": forecast_days,
        "timeformat": "unixtime",
        "timezone": "GMT",
        "domains": "cams_global",
        "cell_selection": "nearest",
    }
    result = fetch(SOURCE_KEY, AQ_URL, params=params, meta={"kind": "air_quality", **(meta or {})})
    series = parse_series(result.data, points)
    set_fetch_records(result.fetch_id, sum(len(s.times) for s in series))
    return series, result
