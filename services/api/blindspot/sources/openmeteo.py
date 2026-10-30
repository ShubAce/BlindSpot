"""Open-Meteo: winds at four heights, boundary-layer height and surface fields on a lon/lat grid.

Pure fetch + parse, no database writes (blindspot/jobs/met.py stores what this returns).

Why the request looks the way it does (each point verified live in
docs/research/meteo-and-trajectories.md section 1 and re-checked in SYNTHESIS.md section 1 #4):
- POST, not GET. A 400-point grid needs about 5 KB of URL and 1,000 points about 12 KB; GET fails
  with HTTP 414 past ~8.2 KB. Open-Meteo accepts the same parameters as a form body.
- `wind_u_component_<level>` / `wind_v_component_<level>`: not in the docs, but they work for 10 m
  and every hPa level. Averaging u and v avoids the 359 deg -> 1 deg wrap-around you get when you
  interpolate wind directions.
- `cell_selection=nearest`, so a grid node is never moved to the nearest "land" cell.
- One model per request. Open-Meteo counts a request as about (locations x variables x models) / 10
  calls (minimum 1 per location), and the free tier allows 600 calls a minute, so two models in one
  request could be refused. The caller spaces the requests instead.
- The answer is a JSON array in request order; its first element has no `location_id`, so we rely
  on the order, never on that field.

Licence: CC BY 4.0, free tier non-commercial. Credit "Weather data by Open-Meteo.com".
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import numpy as np

from blindspot.config import BBox
from blindspot.http import fetch, set_fetch_records
from blindspot.provenance import SourceInfo, register_source

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
META_URL = "https://api.open-meteo.com/data/{domain}/static/meta.json"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# The four heights we read the wind at. 950/925/850 hPa sit about 350, 580 and 1,300 m above the
# Punjab plains (research section 1, "Height of each level"); smoke from field fires mixes through
# that layer by day, which is why one surface wind is not enough.
LEVELS: tuple[str, ...] = ("10m", "950hPa", "925hPa", "850hPa")
PRESSURE_LEVELS_HPA: dict[str, float] = {"950hPa": 950.0, "925hPa": 925.0, "850hPa": 850.0}
WIND_VARIABLES: tuple[str, ...] = tuple(
    f"wind_{component}_component_{level}" for level in LEVELS for component in ("u", "v")
)

# A large grid answer is ~10 MB of JSON; give it more than the default 30 s read timeout.
GRID_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

register_source(
    SourceInfo(
        "openmeteo",
        "Open-Meteo forecast API (winds at 10 m/950/925/850 hPa, boundary layer; CC BY 4.0)",
        "weather",
        3 * 60,
        "https://open-meteo.com/en/docs",
    )
)
register_source(
    SourceInfo(
        "openmeteo_geocoding",
        "Open-Meteo geocoding API (GeoNames places)",
        "places",
        None,
        "https://open-meteo.com/en/docs/geocoding-api",
    )
)


# --- the grid -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GridSpec:
    """A regular lon/lat grid. Node (j, i) is at (lat0 + j*dlat, lon0 + i*dlon); arrays are stored
    row-major from the south-west corner: index = j * nx + i (rows go south to north)."""

    lon0: float
    lat0: float
    dlon: float
    dlat: float
    nx: int
    ny: int

    @property
    def n(self) -> int:
        return self.nx * self.ny

    @property
    def bbox(self) -> BBox:
        return (
            self.lon0,
            self.lat0,
            self.lon0 + self.dlon * (self.nx - 1),
            self.lat0 + self.dlat * (self.ny - 1),
        )

    def lons(self) -> np.ndarray:
        return self.lon0 + self.dlon * np.arange(self.nx)

    def lats(self) -> np.ndarray:
        return self.lat0 + self.dlat * np.arange(self.ny)

    def points(self) -> list[tuple[float, float]]:
        """(lat, lon) of every node in storage order."""
        return [(float(lat), float(lon)) for lat in self.lats() for lon in self.lons()]

    def as_dict(self) -> dict[str, float | int]:
        return {
            "lon0": self.lon0, "lat0": self.lat0, "dlon": self.dlon, "dlat": self.dlat,
            "nx": self.nx, "ny": self.ny,
        }

    @classmethod
    def covering(
        cls,
        bbox: BBox,
        max_points: int = 450,
        steps: Sequence[float] = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0),
    ) -> GridSpec:
        """The finest grid from `steps` that covers `bbox` with at most `max_points` nodes.

        450 keeps one model's request under ~550 "calls" (12 variables count 1.2 per location), so
        one request never exceeds the free tier's 600 calls a minute. Research: grid spacing barely
        matters (0.5 -> 1 deg moved a 24 h trajectory 12 km), the domain size does (SYNTHESIS: wide
        box at 0.75 deg = 425 points).
        """
        min_lon, min_lat, max_lon, max_lat = bbox
        for step in steps:
            nx = math.ceil(round((max_lon - min_lon) / step, 6)) + 1
            ny = math.ceil(round((max_lat - min_lat) / step, 6)) + 1
            if nx * ny <= max_points:
                return cls(float(min_lon), float(min_lat), float(step), float(step), nx, ny)
        raise ValueError(f"bbox {bbox} needs more than {max_points} points even at {steps[-1]} deg")


@dataclass
class ModelGrid:
    """One model's hourly fields on a GridSpec. Each field is float32 with shape (T, ny, nx)."""

    model: str
    spec: GridSpec
    times: np.ndarray  # int64 unix seconds, hourly
    fields: dict[str, np.ndarray]
    elevation: np.ndarray  # (ny, nx) metres, Open-Meteo's DEM height at each requested node
    fetch_id: int | None
    fetched_at: datetime | None
    url_logged: str
    stats: dict[str, Any] = field(default_factory=dict)


def _form(spec: GridSpec, model: str, hourly: Sequence[str], past_days: int, forecast_days: int) -> dict[str, str]:
    points = spec.points()
    return {
        "latitude": ",".join(f"{lat:g}" for lat, _ in points),
        "longitude": ",".join(f"{lon:g}" for _, lon in points),
        "hourly": ",".join(hourly),
        "models": model,
        "past_days": str(past_days),
        "forecast_days": str(forecast_days),
        "wind_speed_unit": "ms",  # the default is km/h, and that applies to u/v components too
        "timezone": "GMT",
        "timeformat": "unixtime",
        "cell_selection": "nearest",
    }


def parse_grid(
    payload: Any, spec: GridSpec, hourly: Sequence[str]
) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray, dict[str, Any]]:
    """Turn Open-Meteo's array of locations into (times, {variable: (T, ny, nx)}, elevation, qa).

    Raises ValueError when the answer does not match the request (wrong number of locations,
    misaligned time axes, a missing variable), so a bad answer is never stored as data.
    Nulls become NaN; qa reports how many there were and how far the returned model cells sit
    from the requested nodes.
    """
    locations = payload if isinstance(payload, list) else [payload]
    if len(locations) != spec.n:
        raise ValueError(f"asked for {spec.n} locations, got {len(locations)}")
    times = np.asarray(locations[0]["hourly"]["time"], dtype=np.int64)
    if times.size < 2:
        raise ValueError("fewer than two time steps returned")
    steps = np.diff(times)
    if not np.all(steps == steps[0]):
        raise ValueError("time axis is not regular")
    for loc in locations:
        hours = loc["hourly"]["time"]
        if len(hours) != times.size or hours[0] != times[0] or hours[-1] != times[-1]:
            raise ValueError("locations returned different time axes")
    fields: dict[str, np.ndarray] = {}
    nan_counts: dict[str, int] = {}
    for name in hourly:
        try:
            rows = [loc["hourly"][name] for loc in locations]
        except KeyError:
            raise ValueError(f"variable {name!r} missing from the answer") from None
        values = np.array(rows, dtype=np.float32)  # (n, T); None -> NaN
        fields[name] = np.ascontiguousarray(values.T.reshape(times.size, spec.ny, spec.nx))
        nan_counts[name] = int(np.isnan(values).sum())
    elevation = np.array(
        [loc.get("elevation") for loc in locations], dtype=np.float32
    ).reshape(spec.ny, spec.nx)
    requested = np.array(spec.points(), dtype=np.float64)
    returned = np.array([(loc["latitude"], loc["longitude"]) for loc in locations], dtype=np.float64)
    qa = {
        "nan_values": {k: v for k, v in nan_counts.items() if v},
        "max_cell_offset_deg": round(float(np.abs(returned - requested).max()), 4),
        "time_step_s": int(steps[0]),
    }
    return times, fields, elevation, qa


def fetch_grid(
    spec: GridSpec,
    model: str,
    hourly: Sequence[str],
    *,
    past_days: int,
    forecast_days: int,
) -> ModelGrid:
    """POST one model's hourly fields for every node of `spec` (one source_fetch row)."""
    result = fetch(
        "openmeteo",
        FORECAST_URL,
        method="POST",
        data=_form(spec, model, hourly, past_days, forecast_days),
        timeout=GRID_TIMEOUT,
        meta={
            "model": model,
            "points": spec.n,
            "grid": spec.as_dict(),
            "hourly": list(hourly),
            "past_days": past_days,
            "forecast_days": forecast_days,
        },
    )
    times, fields, elevation, qa = parse_grid(result.data, spec, hourly)
    # One record = one grid node for one hour (all variables).
    set_fetch_records(result.fetch_id, spec.n * int(times.size), meta={"qa": qa})
    stats = {**qa, "latency_ms": result.latency_ms, "hours": int(times.size), "points": spec.n}
    return ModelGrid(
        model=model,
        spec=spec,
        times=times,
        fields=fields,
        elevation=elevation,
        fetch_id=result.fetch_id,
        fetched_at=result.fetched_at,
        url_logged=result.url_logged,
        stats=stats,
    )


# --- model-run metadata -----------------------------------------------------------------------------


def _utc(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def fetch_model_meta(domain: str) -> dict[str, Any]:
    """When the latest run of an upstream model (e.g. `ecmwf_ifs`, `dwd_icon`, `ncep_gfs013`) was
    initialised and became available on Open-Meteo. Lets the met job refetch only when a new run
    has landed, which keeps us well inside the free tier."""
    result = fetch("openmeteo", META_URL.format(domain=domain), meta={"what": "model run metadata"})
    data = result.data or {}
    set_fetch_records(result.fetch_id, 1)
    return {
        "domain": domain,
        "init": _utc(data.get("last_run_initialisation_time")),
        "available": _utc(data.get("last_run_availability_time")),
        "data_end": _utc(data.get("data_end_time")),
        "update_interval_s": data.get("update_interval_seconds"),
        "fetch_id": result.fetch_id,
    }


# --- places (fallback only) -------------------------------------------------------------------------


def geocode_city(name: str, country_code: str = "IN") -> dict[str, Any] | None:
    """The most populous GeoNames place called `name` in the country, or None.

    Only used when the boundaries module has not loaded `place` rows of kind 'city' yet; the
    returned coordinates and population come from GeoNames through Open-Meteo, never from us.
    """
    result = fetch(
        "openmeteo_geocoding",
        GEOCODE_URL,
        params={"name": name, "count": 10, "language": "en", "format": "json", "countryCode": country_code},
        meta={"name": name},
    )
    candidates = [r for r in (result.data or {}).get("results", []) if r.get("country_code") == country_code]
    set_fetch_records(result.fetch_id, len(candidates))
    if not candidates:
        return None
    best = max(candidates, key=lambda r: r.get("population") or 0)
    return {
        "name": name,
        "lat": float(best["latitude"]),
        "lon": float(best["longitude"]),
        "state": best.get("admin1"),
        "district": best.get("admin2"),
        "population": best.get("population"),
        "geonames_id": best.get("id"),
        "fetch_id": result.fetch_id,
    }
