"""Air quality: what the CPCB stations measure now, one number per city, hourly city series
against the CAMS model, and IITM DSS's stubble-share benchmark. Reads the database only; the air
jobs (blindspot/jobs/air.py) fill it.

The rules every number here follows:
- A city's "now" PM2.5 is the median of its stations that reported in the last 2 hours, served
  with how many stations that is. A station that has gone quiet is listed with stale=true and its
  age, never silently dropped.
- Measurements and the CAMS model are separate numbers. They are never averaged together.
- CPCB's data.gov.in values are AQI sub-indices, so they appear only under `aqi`, never as µg/m³.
- Hourly series use IST clock hours, as CPCB does; `t` is the end of the hour, in UTC.
- `at` (optional, ISO time) replays any endpoint as of a past moment, on the same stored data.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from blindspot.api import BBoxDep, ORJSONResponse, SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.geo import AOI_BBOX, IST
from blindspot.models.air import (
    CAMS,
    DATAGOV,
    FRESH_MINUTES,
    OPENAQ,
    UGM3,
    CpcbIndex,
    DssSnapshot,
    StationRef,
    canonical_city,
)
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch, utcnow
from blindspot.sources.datagov import aqi_category, station_aqi, subindex_to_concentration
from blindspot.sources.iitm_dss import DSS_URL, STUBBLE
from blindspot.sources.iitm_dss import SOURCE_KEY as DSS_KEY

router = APIRouter(prefix="/api/air", tags=["air"])

LISTED_FOR = timedelta(hours=72)  # a station silent for longer is no longer listed
# A 24-hour mean needs 18 of its 24 hourly values (75 % data capture, the usual validity rule
# for daily means); a "daily" mean of a few hours would mislead.
MIN_HOURS_FOR_DAILY_MEAN = 18


def _as_of(at: datetime | None) -> datetime:
    """The moment to answer for: now, or `at` (naive means UTC). A later `at` just ages the
    stored data (stations turn stale); nothing is extrapolated."""
    if at is None:
        return utcnow()
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


def _last_full_hour(at: datetime) -> datetime:
    """End of the last complete IST clock hour at or before `at`, in UTC (IST is UTC+5:30, so
    CPCB's hours end at :30 UTC)."""
    return at.astimezone(IST).replace(minute=0, second=0, microsecond=0).astimezone(UTC)


def _minutes(earlier: datetime, later: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)


def _refs(session: Session, fetch_ids: dict[str, int], fallback: list[str]) -> list[SourceRef]:
    """The newest fetch behind the served numbers of each provider; when nothing was served,
    the latest fetch of each source (nulls if never fetched), so the UI can say "no data yet"."""
    refs = [ref for fid in fetch_ids.values() if (ref := ref_for_fetch(session, fid)) is not None]
    return refs or latest_refs(session, fallback)


# --- /latest ------------------------------------------------------------------------------------

_LATEST_SQL = text(
    """
    SELECT DISTINCT ON (provider, station_id, parameter)
           provider, station_id, station_name, city, state, lat, lon, parameter, value,
           observed_at, is_model, fetch_id
    FROM air_obs
    WHERE observed_at <= :at AND observed_at > :since
      AND lon BETWEEN :min_lon AND :max_lon AND lat BETWEEN :min_lat AND :max_lat
    ORDER BY provider, station_id, parameter, observed_at DESC, period_minutes
    """
)

# Per city, the mean over the last 24 complete IST hours of the hourly city value (the median of
# its stations' hourly PM2.5), and how many of those hours had any station.
_DAILY_MEAN_SQL = text(
    """
    WITH obs AS (
        SELECT city, station_id, period_minutes, value,
               (date_trunc('hour', (observed_at AT TIME ZONE 'Asia/Kolkata')
                                   - interval '1 microsecond') + interval '1 hour')
                   AT TIME ZONE 'Asia/Kolkata' AS t
        FROM air_obs
        WHERE provider = :provider AND parameter = 'pm25' AND city IS NOT NULL
          AND observed_at > :t_end - interval '24 hours' AND observed_at <= :t_end
    ),
    best AS (  -- per station and hour: the hourly mean when we have it, else the 15-min readings
        SELECT city, station_id, t, max(period_minutes) AS period FROM obs GROUP BY 1, 2, 3
    ),
    newest_hourly AS (
        SELECT station_id, max(t) AS t60 FROM obs WHERE period_minutes = 60 GROUP BY 1
    ),
    per_station AS (  -- 15-min readings only stand in for hours OpenAQ has not averaged yet; an
                      -- older hour without a mean failed the 75 % capture rule and stays a gap
        SELECT o.city, o.station_id, o.t, avg(o.value) AS v
        FROM obs o JOIN best b
          ON b.city = o.city AND b.station_id = o.station_id AND b.t = o.t
         AND b.period = o.period_minutes
        LEFT JOIN newest_hourly n ON n.station_id = b.station_id
        WHERE b.period = 60 OR n.t60 IS NULL OR b.t > n.t60
        GROUP BY 1, 2, 3
    ),
    city_hour AS (
        SELECT city, t, percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS v
        FROM per_station GROUP BY 1, 2
    )
    SELECT city, avg(v) AS mean, count(*) AS hours FROM city_hour GROUP BY city
    """
)


@dataclass
class _Station:
    """The newest value of each parameter of one station (one provider's view of it)."""

    provider: str
    station_id: str
    name: str
    city: str | None
    state: str | None
    lat: float
    lon: float
    is_model: bool
    values: dict[str, tuple[float, datetime]] = field(default_factory=dict)  # param -> (v, t)

    def newest(self) -> datetime:
        return max(t for _v, t in self.values.values())

    def get(self, parameter: str) -> tuple[float | None, datetime | None]:
        value, when = self.values.get(parameter, (None, None))
        return value, when


def _load_stations(
    session: Session, at: datetime, bbox: tuple[float, float, float, float]
) -> tuple[list[_Station], dict[str, int]]:
    """Every station (and model point) in bbox with data in the listing window, and per provider
    the newest fetch behind the values."""
    params = {
        "at": at,
        "since": at - LISTED_FOR,
        "min_lon": bbox[0],
        "min_lat": bbox[1],
        "max_lon": bbox[2],
        "max_lat": bbox[3],
    }
    stations: dict[tuple[str, str], _Station] = {}
    fetch_ids: dict[str, int] = {}
    for r in session.execute(_LATEST_SQL, params):
        key = (r.provider, r.station_id)
        if key not in stations:
            stations[key] = _Station(
                r.provider, r.station_id, r.station_name, r.city, r.state, r.lat, r.lon, r.is_model
            )
        stations[key].values[r.parameter] = (r.value, r.observed_at)
        if r.fetch_id is not None:
            fetch_ids[r.provider] = max(fetch_ids.get(r.provider, 0), r.fetch_id)
    return list(stations.values()), fetch_ids


def _aqi(station: _Station, at: datetime) -> dict[str, Any]:
    """A data.gov.in station's AQI by CPCB's rule, from the sub-indices of its newest batch."""
    batch = station.newest()
    subindex = {
        parameter.removesuffix("_subindex"): value
        for parameter, (value, when) in station.values.items()
        if parameter.endswith("_subindex") and when == batch
    }
    aqi, prominent = station_aqi(subindex)
    pm25_si = subindex.get("pm25")
    return {
        "value": aqi,
        "category": aqi_category(aqi),
        "prominent": prominent,
        "subindex": {k: int(v) for k, v in sorted(subindex.items())},
        "pm25_24h_from_subindex": (
            subindex_to_concentration("pm25", pm25_si) if pm25_si is not None else None
        ),
        "observed_at": batch,
        "age_minutes": _minutes(batch, at),
        "stale": _minutes(batch, at) > FRESH_MINUTES,
        "provider": DATAGOV,
        "station": station.name,
    }


def _station_json(station: _Station, at: datetime, aqi: dict[str, Any] | None) -> dict[str, Any]:
    pm25, pm25_at = station.get("pm25")
    pm10, pm10_at = station.get("pm10")
    times = [t for t in (pm25_at, pm10_at) if t is not None]
    observed = max(times) if times else station.newest()  # a CPCB-feed station: its AQI batch
    prefix = {OPENAQ: "openaq", DATAGOV: "cpcb", CAMS: "cams"}[station.provider]
    return {
        "id": f"{prefix}:{station.station_id}",
        "name": station.name,
        "city": station.city,
        "state": station.state,
        "lat": station.lat,
        "lon": station.lon,
        "pm25": pm25,
        "pm10": pm10,
        "observed_at": observed,
        "provider": station.provider,
        "is_model": station.is_model,
        "pm25_observed_at": pm25_at,
        "pm10_observed_at": pm10_at,
        "age_minutes": _minutes(observed, at),
        "stale": _minutes(observed, at) > FRESH_MINUTES,
        "aqi": aqi,
    }


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _fresh(stations: list[_Station], parameter: str, at: datetime) -> list[tuple[float, datetime]]:
    """(value, time) of each station whose newest `parameter` is at most FRESH_MINUTES old."""
    out = []
    for s in stations:
        value, when = s.get(parameter)
        if value is not None and when is not None and _minutes(when, at) <= FRESH_MINUTES:
            out.append((value, when))
    return out


def _last_known(measured: list[_Station], at: datetime) -> dict[str, Any] | None:
    """The city's PM2.5 at its newest observation: the median of the stations that reported
    within FRESH_MINUTES of that moment, with its age. OpenAQ republishes CPCB data in hourly
    batches 1-2 h late, so for part of an hour no station may count as "now"; this lets the UI
    still show the latest real value, clearly aged, instead of a blank. Never used as pm25_now."""
    readings = [(v, t) for s in measured for v, t in [s.get("pm25")] if v is not None and t]
    if not readings:
        return None
    newest = max(t for _v, t in readings)
    values = [v for v, t in readings if _minutes(t, newest) <= FRESH_MINUTES]
    return {
        "value": _median(values),
        "observed_at": newest,
        "n_stations": len(values),
        "age_minutes": _minutes(newest, at),
        "stale": _minutes(newest, at) > FRESH_MINUTES,
    }


def _city_aqi(cpcb: list[_Station], aqis: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """CPCB's city AQI: the mean of its stations' AQIs (CPCB AQI bulletin note), fresh ones only."""
    found = [
        a for s in cpcb if (a := aqis.get(s.name)) and a["value"] is not None and not a["stale"]
    ]
    if not found:
        return None
    value = round(statistics.mean(a["value"] for a in found))
    return {
        "value": value,
        "category": aqi_category(value),
        "n_stations": len(found),
        "observed_at": max(a["observed_at"] for a in found),
        "provider": DATAGOV,
    }


def _receptors(
    session: Session,
    stations: list[_Station],
    aqis: dict[str, dict[str, Any]],
    at: datetime,
) -> list[dict[str, Any]]:
    """One entry per city: measured PM now (median of fresh stations), its 24-hour mean, the
    city AQI from CPCB's feed, and the CAMS model value, side by side and never blended."""
    daily = {
        r.city: (r.mean, r.hours)
        for r in session.execute(
            _DAILY_MEAN_SQL, {"provider": OPENAQ, "t_end": _last_full_hour(at)}
        )
    }
    by_city: dict[str, dict[str, list[_Station]]] = defaultdict(lambda: defaultdict(list))
    for s in stations:
        if s.city:
            by_city[s.city][s.provider].append(s)
    out = []
    for city, groups in by_city.items():
        measured = groups.get(OPENAQ, [])
        cpcb = groups.get(DATAGOV, [])
        if not measured and not cpcb:
            continue  # a model point alone is not a monitored city
        pm25, pm10 = _fresh(measured, "pm25", at), _fresh(measured, "pm10", at)
        mean, hours = daily.get(city, (None, 0))
        model = next(iter(groups.get(CAMS, [])), None)
        everyone = measured + cpcb
        out.append(
            {
                "city": city,
                "state": next((s.state for s in everyone if s.state), None),
                "pm25_now": _median([v for v, _t in pm25]),
                "pm25_24h_mean": round(mean, 1)
                if mean is not None and hours >= MIN_HOURS_FOR_DAILY_MEAN
                else None,
                "observed_at": max((t for _v, t in pm25), default=None),
                "provider": OPENAQ,
                "n_stations": len(pm25),
                "pm10_now": _median([v for v, _t in pm10]),
                "n_stations_pm10": len(pm10),
                "n_stations_total": len(measured),
                "n_stale": sum(1 for s in measured if _minutes(s.newest(), at) > FRESH_MINUTES),
                "pm25_24h_hours": int(hours),
                "pm25_last": _last_known(measured, at),
                "last_observed_at": max((s.newest() for s in measured), default=None),
                "lat": round(statistics.median(s.lat for s in everyone), 4),
                "lon": round(statistics.median(s.lon for s in everyone), 4),
                "aqi": _city_aqi(cpcb, aqis),
                "model": {
                    "pm25": model.get("pm25")[0],
                    "pm10": model.get("pm10")[0],
                    "valid_at": model.newest(),
                    "provider": CAMS,
                }
                if model is not None
                else None,
            }
        )
    out.sort(
        key=lambda r: (-(r["n_stations_total"] + (r["aqi"] or {}).get("n_stations", 0)), r["city"])
    )
    return out


def air_latest(
    session: Session,
    at: datetime | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[dict[str, Any], list[SourceRef]]:
    """Stations and per-city receptors as of `at` (the payload of GET /api/air/latest)."""
    at = _as_of(at)
    stations, fetch_ids = _load_stations(session, at, bbox or AOI_BBOX)
    cpcb = [s for s in stations if s.provider == DATAGOV]
    aqis = {s.name: _aqi(s, at) for s in cpcb}
    index = CpcbIndex(StationRef(s.name, s.city, s.state, s.lat, s.lon) for s in cpcb)
    measured = [s for s in stations if s.provider == OPENAQ]
    twins = index.match_all((s.station_id, s.name, s.lat, s.lon) for s in measured)
    matched = {twin.name for twin in twins.values()}
    listed: list[dict[str, Any]] = []
    for s in stations:
        if s.provider == OPENAQ:
            twin = twins.get(s.station_id)
            listed.append(_station_json(s, at, aqis.get(twin.name) if twin else None))
        elif s.provider == CAMS:
            listed.append(_station_json(s, at, None))
    # CPCB-feed stations OpenAQ does not carry: listed with their AQI only (no µg/m³).
    listed += [_station_json(s, at, aqis[s.name]) for s in cpcb if s.name not in matched]
    listed.sort(key=lambda x: (x["is_model"], x["city"] or "~", x["name"]))
    receptors = _receptors(session, stations, aqis, at)
    observed = [x for x in listed if not x["is_model"]]
    payload = {
        "as_of": at,
        "fresh_minutes": FRESH_MINUTES,
        "stations": listed,
        "receptors": receptors,
        "counts": {
            "stations": len(observed),
            "fresh": sum(1 for x in observed if not x["stale"]),
            "stale": sum(1 for x in observed if x["stale"]),
            "with_pm25": sum(1 for x in observed if x["pm25"] is not None),
            "model_points": len(listed) - len(observed),
            "cities": len(receptors),
        },
    }
    if not observed:
        payload["reason"] = "no station data in the database for this time and area yet"
    return payload, _refs(session, fetch_ids, [OPENAQ, DATAGOV, CAMS])


_CITY_SQL = text(
    f"""
    WITH latest AS ({_LATEST_SQL.text})
    SELECT * FROM latest
    WHERE lower(city) = lower(:city)
       OR (provider = :datagov AND station_name IN (
           SELECT station_name FROM latest WHERE lower(city) = lower(:city) AND provider = :datagov))
    ORDER BY provider, station_id, parameter
    """
)
_FETCHES_SQL = text(
    f"""
    WITH latest AS ({_LATEST_SQL.text})
    SELECT provider, max(fetch_id) AS fetch_id FROM latest GROUP BY provider ORDER BY provider
    """
)


def air_city(
    session: Session, city: str, at: datetime | None = None
) -> tuple[dict[str, Any] | None, list[SourceRef]]:
    """One city's receptor entry and the sources, exactly as air_latest() gives them (the
    headline's Delhi block), reading that city's stations only: air_latest reads every station in
    the area (~150 KB of rows) to answer for one city. CPCB-feed stations elsewhere that share a
    name with one of the city's are read too, because the city AQI looks stations up by name."""
    at = _as_of(at)
    bbox = AOI_BBOX
    params = {"at": at, "since": at - LISTED_FOR, "min_lon": bbox[0], "min_lat": bbox[1],
              "max_lon": bbox[2], "max_lat": bbox[3]}
    stations: dict[tuple[str, str], _Station] = {}
    for r in session.execute(_CITY_SQL, {**params, "city": city, "datagov": DATAGOV}):
        key = (r.provider, r.station_id)
        if key not in stations:
            stations[key] = _Station(
                r.provider, r.station_id, r.station_name, r.city, r.state, r.lat, r.lon, r.is_model
            )
        stations[key].values[r.parameter] = (r.value, r.observed_at)
    fetch_ids = {r.provider: r.fetch_id for r in session.execute(_FETCHES_SQL, params) if r.fetch_id is not None}
    found = list(stations.values())
    aqis = {s.name: _aqi(s, at) for s in found if s.provider == DATAGOV}
    receptors = _receptors(session, found, aqis, at)
    entry = next((r for r in receptors if (r.get("city") or "").lower() == city.lower()), None)
    return entry, _refs(session, fetch_ids, [OPENAQ, DATAGOV, CAMS])


@router.get("/latest")
@cached_route(ttl=30)
def latest(session: SessionDep, bbox: BBoxDep, at: datetime | None = None) -> ORJSONResponse:
    """Every CPCB station in the area with its newest PM2.5/PM10 (µg/m³, via OpenAQ) and its
    official AQI (CPCB feed), the CAMS model at each city, and one receptor entry per city."""
    payload, sources = air_latest(session, at, bbox)
    return respond(payload, sources, max_age=60)


# --- /series ------------------------------------------------------------------------------------

_SERIES_SQL = text(
    """
    WITH grid AS (
        SELECT generate_series(:t_end - (:hours - 1) * interval '1 hour', :t_end,
                               interval '1 hour') AS t
    ),
    obs AS (
        SELECT station_id, period_minutes, value,
               (date_trunc('hour', (observed_at AT TIME ZONE 'Asia/Kolkata')
                                   - interval '1 microsecond') + interval '1 hour')
                   AT TIME ZONE 'Asia/Kolkata' AS t
        FROM air_obs
        WHERE provider = :provider AND parameter = 'pm25' AND city = :city
          AND observed_at > :t_end - :hours * interval '1 hour' AND observed_at <= :t_end
    ),
    best AS (
        SELECT station_id, t, max(period_minutes) AS period FROM obs GROUP BY 1, 2
    ),
    newest_hourly AS (
        SELECT station_id, max(t) AS t60 FROM obs WHERE period_minutes = 60 GROUP BY 1
    ),
    per_station AS (  -- as in _DAILY_MEAN_SQL: 15-min readings only for hours not yet averaged
        SELECT o.station_id, o.t, avg(o.value) AS v
        FROM obs o JOIN best b
          ON b.station_id = o.station_id AND b.t = o.t AND b.period = o.period_minutes
        LEFT JOIN newest_hourly n ON n.station_id = b.station_id
        WHERE b.period = 60 OR n.t60 IS NULL OR b.t > n.t60
        GROUP BY 1, 2
    ),
    city_hour AS (
        SELECT t, percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS pm25, count(*) AS n
        FROM per_station GROUP BY t
    )
    SELECT g.t AS hour_end, c.pm25, COALESCE(c.n, 0) AS n_stations
    FROM grid g LEFT JOIN city_hour c ON c.t = g.t
    ORDER BY g.t
    """
)

_MODEL_SQL = text(
    """
    SELECT observed_at AS valid_at, value AS pm25, fetch_id FROM air_obs
    WHERE provider = :provider AND station_id = :city AND parameter = 'pm25'
      AND observed_at > :since AND observed_at <= :until
    ORDER BY observed_at
    """
)

_SERIES_FETCH_SQL = text(
    """
    SELECT max(fetch_id) FROM air_obs
    WHERE provider = :provider AND parameter = 'pm25' AND city = :city
      AND observed_at > :since AND observed_at <= :until
    """
)


def resolve_city(session: Session, city: str) -> str | None:
    """The stored spelling of a city name ("delhi", "New Delhi" -> "Delhi"), or None."""
    wanted = canonical_city(city)
    if not wanted:
        return None
    return session.execute(
        text("SELECT city FROM air_obs WHERE lower(city) = lower(:city) LIMIT 1"),
        {"city": wanted},
    ).scalar()


def air_series(
    session: Session, city: str, hours: int = 72, ahead_hours: int = 0, at: datetime | None = None
) -> tuple[dict[str, Any], list[SourceRef]] | None:
    """Hourly city PM2.5 and the CAMS model for a city (GET /api/air/series), None if unknown."""
    name = resolve_city(session, city)
    if name is None:
        return None
    at = _as_of(at)
    t_end = _last_full_hour(at)
    params = {"provider": OPENAQ, "city": name, "t_end": t_end, "hours": hours}
    series = [
        {
            "t": r.hour_end,
            "pm25": round(r.pm25, 1) if r.pm25 is not None else None,
            "n_stations": r.n_stations,
        }
        for r in session.execute(_SERIES_SQL, params)
    ]
    model_rows = session.execute(
        _MODEL_SQL,
        {
            "provider": CAMS,
            "city": name,
            "since": at - timedelta(hours=hours),
            "until": at + timedelta(hours=ahead_hours),
        },
    ).all()
    model_series = [{"t": r.valid_at, "pm25": r.pm25} for r in model_rows if r.valid_at <= at]
    forecast = [{"t": r.valid_at, "pm25": r.pm25} for r in model_rows if r.valid_at > at]
    window = {"since": t_end - timedelta(hours=hours), "until": t_end, "city": name}
    fetch_ids = {}
    if fid := session.execute(_SERIES_FETCH_SQL, {**window, "provider": OPENAQ}).scalar():
        fetch_ids[OPENAQ] = fid
    model_fetch = max((r.fetch_id for r in model_rows if r.fetch_id is not None), default=None)
    if model_fetch is not None:
        fetch_ids[CAMS] = model_fetch
    with_data = sum(1 for p in series if p["pm25"] is not None)
    payload: dict[str, Any] = {
        "city": name,
        "hours": hours,
        "unit": UGM3,
        "as_of": t_end,
        "series": series,
        "model_series": model_series,
        "provider": OPENAQ,
        "model_provider": CAMS,
        "hours_with_data": with_data,
        "max_stations": max((p["n_stations"] for p in series), default=0),
    }
    if ahead_hours:
        payload["model_forecast"] = forecast
    if not with_data:
        payload["reason"] = f"no station in {name} reported PM2.5 in this window"
    return payload, _refs(session, fetch_ids, [OPENAQ, CAMS])


@router.get("/series")
@cached_route(ttl=60)
def series(
    session: SessionDep,
    city: str,
    hours: int = Query(72, ge=1, le=24 * 14),
    ahead_hours: int = Query(0, ge=0, le=120),
    at: datetime | None = None,
) -> ORJSONResponse:
    """Hourly PM2.5 of a city (median of its stations, IST hours) and the CAMS model alongside;
    `ahead_hours` adds the model's forecast. 404 for a city with no data at all."""
    found = air_series(session, city, hours, ahead_hours, at)
    if found is None:
        known = (
            session.execute(
                text(
                    "SELECT DISTINCT city FROM air_obs WHERE city IS NOT NULL AND NOT is_model "
                    "AND observed_at > now() - interval '7 days' ORDER BY city"
                )
            )
            .scalars()
            .all()
        )
        raise HTTPException(
            404,
            f"no air-quality data for city {city!r}; cities with data: "
            f"{', '.join(known) or 'none yet'}",
        )
    payload, sources = found
    return respond(payload, sources, max_age=120)


# --- /stubble-share -----------------------------------------------------------------------------


def stubble_share(session: Session) -> tuple[dict[str, Any], list[SourceRef]]:
    """The newest IITM DSS numbers we hold (GET /api/air/stubble-share)."""
    row = session.scalars(
        select(DssSnapshot).order_by(DssSnapshot.last_seen_at.desc(), DssSnapshot.id.desc())
    ).first()
    attribution = {
        "label": "IITM Decision Support System (DSS) for Delhi, IITM Pune",
        "url": DSS_URL,
    }
    if row is None:
        empty = {
            "run_date": None,
            "stale": None,
            "today": None,
            "daily": [],
            "outlook": [],
            "method_note": None,
            "attribution": attribution,
            "reason": "IITM DSS has not been read yet",
        }
        return empty, latest_refs(session, [DSS_KEY])
    run_day = row.run_date.isoformat()
    outlook = []
    for day, shares in sorted(row.forecast_pct.items()):
        ranked = sorted(shares.items(), key=lambda kv: -kv[1])
        outlook.append(
            {
                "date": day,
                "stubble_pct": shares.get(STUBBLE),
                # IITM: day 0 uses the previous evening's VIIRS fire counts; the next three days use
                # climatological fire counts and emissions (quoted in method_note).
                "fire_input": "viirs_previous_evening" if day == run_day else "climatology",
                "sources": [{"name": k, "pct": v} for k, v in ranked],
            }
        )
    today_ist = utcnow().astimezone(IST).date()
    payload = {
        "run_date": run_day,
        "stale": row.run_date < today_ist,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "today": {"date": run_day, "stubble_pct": row.forecast_pct.get(run_day, {}).get(STUBBLE)},
        "daily": [{"date": d, "stubble_pct": v} for d, v in sorted(row.daily_pct.items())],
        "outlook": outlook,
        "unit": "% of Delhi's daily mean PM2.5",
        "method_note": row.basis,
        "attribution": attribution,
    }
    ref = ref_for_fetch(session, row.fetch_id)
    return payload, [ref] if ref else latest_refs(session, [DSS_KEY])


@router.get("/stubble-share")
@cached_route(ttl=300)
def stubble_share_route(session: SessionDep) -> ORJSONResponse:
    """IITM DSS's daily share of Delhi's PM2.5 from stubble burning (season to date) and its
    4-day source outlook: the official, VIIRS-driven estimate BlindSpot validates against."""
    payload, sources = stubble_share(session)
    return respond(payload, sources, max_age=600)
