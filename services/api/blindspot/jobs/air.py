"""Air-quality ingest jobs.

`air` (every 30 min) runs three independent steps; one failing never stops the others:
1. data.gov.in, CPCB's official feed (needs DATA_GOV_IN_API_KEY): every station's NAQI
   sub-indices inside the AOI -> air_obs, provider datagov_aqi. The feed keeps no history, so
   archiving it every run is the only way to have one.
2. OpenAQ (needs OPENAQ_API_KEY): the CPCB stations inside the AOI. Each run stores every
   station's latest 15-minute PM2.5 and PM10, then hourly PM2.5 means: 7 days back the first time
   a station is seen, afterwards just the hours added since (re-reading the last two, which may
   have been incomplete). Stations get their city and state from CPCB's own feed (CpcbIndex).
3. CAMS via Open-Meteo: model PM2.5 / PM10 at every city that has a station, sampled at the
   middle of that city's stations, so model and measurement describe the same place. Fetched
   only when a new model run is out (twice a day), which keeps us far inside the free quota.

`air_dss` (10:20, 14:20 and 20:20 IST) reads IITM's DSS stubble share -> dss_snapshot.

Everything is an upsert keyed on (provider, station, parameter, time, period), so re-running a
job never duplicates a row.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.geo import AOI_BBOX, IST, admin_for_points, point_ewkt
from blindspot.http import FetchError, SourceUnavailable
from blindspot.jobs import job
from blindspot.models.air import (
    CAMS,
    DATAGOV,
    OPENAQ,
    SUBINDEX_UNIT,
    UGM3,
    AirObs,
    CpcbIndex,
    DssSnapshot,
    StationRef,
    canonical_city,
)
from blindspot.provenance import iso_z, set_fetch_records, utcnow
from blindspot.sources import cams, datagov, iitm_dss, openaq

log = logging.getLogger(__name__)

BACKFILL = timedelta(days=7)  # how far back a new station's hourly history is read
REREAD = timedelta(hours=2)  # re-read the last hours: OpenAQ may have filled them in since
MAX_HOURLY_CALLS = 150  # per run, to keep a run short; stations left over are first next run
CHUNK = 2000  # rows per INSERT statement


# --- writing ------------------------------------------------------------------------------------


def _obs(**fields: Any) -> dict[str, Any]:
    """One air_obs row as a dict, with its PostGIS point."""
    fields["geom"] = point_ewkt(fields["lon"], fields["lat"])
    return fields


def upsert_obs(session: Session, rows: Iterable[dict[str, Any]]) -> int:
    """Insert rows; a row already stored (same key) gets the new value and labels only when
    they changed, so an unchanged value keeps the fetch it first came from. Returns rows sent."""
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:  # one statement must not touch the same key twice
        key = (
            row["provider"],
            row["station_id"],
            row["parameter"],
            row["observed_at"],
            row["period_minutes"],
        )
        unique[key] = row
    batch = list(unique.values())
    if not batch:
        return 0
    table = AirObs.__table__
    stmt = pg_insert(table)
    new = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        constraint="uq_air_obs_key",
        set_={
            name: new[name]
            for name in (
                "value",
                "unit",
                "station_name",
                "city",
                "state",
                "lat",
                "lon",
                "geom",
                "fetch_id",
            )
        },
        where=or_(
            *(table.c[name].is_distinct_from(new[name]) for name in ("value", "city", "state"))
        ),
    )
    for start in range(0, len(batch), CHUNK):
        session.execute(stmt, batch[start : start + CHUNK])
    return len(batch)


# --- step 1: data.gov.in ------------------------------------------------------------------------


def _last_complete_batch(session: Session) -> str | None:
    """The feed stamp of the newest data.gov.in pull whose rows were stored (datagov.mark_stored)."""
    return session.execute(
        text(
            """
            SELECT meta->>'complete_batch' FROM source_fetch
            WHERE source_key = :key AND ok AND meta ? 'complete_batch'
            ORDER BY started_at DESC LIMIT 1
            """
        ),
        {"key": DATAGOV},
    ).scalar()


def datagov_step() -> dict[str, Any]:
    settings = get_settings()
    key, relay = settings.data_gov_in_api_key, settings.datagov_relay_url
    if key is None:
        return {"skipped": "DATA_GOV_IN_API_KEY is not set"}
    stamp = datagov.feed_stamp(key.get_secret_value(), relay)
    with session_scope() as session:
        if stamp is not None and stamp == _last_complete_batch(session):
            return {"skipped": "CPCB feed unchanged since the last pull", "feed_updated": stamp}
    batch = datagov.fetch_all(key.get_secret_value(), AOI_BBOX, relay)
    rows = [
        _obs(
            provider=DATAGOV,
            station_id=r.station,
            station_name=r.station,
            city=canonical_city(r.city),
            state=r.state,
            lat=r.lat,
            lon=r.lon,
            parameter=f"{r.pollutant}_subindex",
            value=float(r.value),
            unit=SUBINDEX_UNIT,
            observed_at=r.observed_at,
            period_minutes=r.window_minutes,
            is_model=False,
            fetch_id=fetch_id,
        )
        for r, fetch_id in zip(batch.rows, batch.row_fetch, strict=True)
    ]
    with session_scope() as session:
        stored = upsert_obs(session, rows)
    datagov.mark_stored(batch)  # only now: the next run may skip an unchanged feed
    newest = max((r.observed_at for r in batch.rows), default=None)
    return {
        "rows": stored,
        "stations": len({r.station for r in batch.rows}),
        "batch_utc": iso_z(newest),
        "feed_updated": batch.updated_date,
        "feed_rows_all_india": batch.total,
        "pages": len(batch.fetch_ids),
    }


# --- step 2: OpenAQ -----------------------------------------------------------------------------


def cpcb_index(session: Session, now: datetime) -> CpcbIndex:
    """CPCB's own station list (names, cities, states), from what the data.gov.in step stored."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (station_id) station_id, city, state, lat, lon
            FROM air_obs
            WHERE provider = :provider AND observed_at > :since
            ORDER BY station_id, observed_at DESC
            """
        ),
        {"provider": DATAGOV, "since": now - BACKFILL},
    )
    return CpcbIndex(StationRef(r.station_id, r.city, r.state, r.lat, r.lon) for r in rows)


def _labels(
    session: Session, locations: list[openaq.Location], index: CpcbIndex
) -> dict[int, tuple[str | None, str | None]]:
    """(city, state) per OpenAQ location; boundaries fill a missing state when they are loaded."""
    labels = {loc.id: index.label(loc.name, loc.lat, loc.lon) for loc in locations}
    missing = [loc for loc in locations if labels[loc.id][1] is None]
    if missing:
        areas = admin_for_points(session, [(loc.lon, loc.lat) for loc in missing])
        for loc, area in zip(missing, areas, strict=True):
            if area is not None and area.state:
                labels[loc.id] = (labels[loc.id][0], area.state)
    return labels


def _last_hourly(session: Session) -> dict[str, datetime]:
    """Per OpenAQ station, the end of the newest hourly PM2.5 mean we hold."""
    rows = session.execute(
        text(
            """
            SELECT station_id, max(observed_at) AS last FROM air_obs
            WHERE provider = :provider AND parameter = 'pm25' AND period_minutes = 60
            GROUP BY station_id
            """
        ),
        {"provider": OPENAQ},
    )
    return {r.station_id: r.last for r in rows}


def hourly_since(last: datetime | None, now: datetime) -> datetime:
    """Where a station's hourly read starts: a week back for a station we hold no hours of (or
    whose newest hour is older than that), otherwise REREAD before its newest stored hour.

    The week-back start is moved to the start of an IST clock hour, where CPCB's hours begin.
    OpenAQ averages only the readings after `datetime_from`, so a start in mid-hour comes back as
    a partial hour posing as an hourly mean (verified 2026-10-30: from 12:16:39Z, the hour ending
    12:30Z was averaged from 1 of its 4 readings); a start exactly on the hour leaves that hour out.
    `last - REREAD` is already on the station's own hour boundary."""
    if last is not None and last - REREAD >= now - BACKFILL:
        return last - REREAD
    start = (now - BACKFILL).astimezone(IST)
    return start.replace(minute=0, second=0, microsecond=0).astimezone(UTC)


def _reading_row(
    reading: openaq.Reading,
    loc: openaq.Location,
    label: tuple[str | None, str | None],
    fetch_id: int | None,
) -> dict[str, Any]:
    return _obs(
        provider=OPENAQ,
        station_id=str(loc.id),
        station_name=loc.name,
        city=label[0],
        state=label[1],
        lat=loc.lat,
        lon=loc.lon,
        parameter=reading.parameter,
        value=reading.value,
        unit=UGM3,
        observed_at=reading.observed_at,
        period_minutes=reading.period_minutes,
        is_model=False,
        fetch_id=fetch_id,
    )


def openaq_step(now: datetime | None = None) -> dict[str, Any]:
    key = get_settings().openaq_api_key
    if key is None:
        return {"skipped": "OPENAQ_API_KEY is not set"}
    now = now or utcnow()
    client = openaq.OpenAQClient(key.get_secret_value())
    listed = client.locations(AOI_BBOX)
    # A location OpenAQ still lists but that has not reported for a week is a retired station.
    live = [
        loc for loc in listed if loc.sensors and loc.last_utc and now - loc.last_utc <= BACKFILL
    ]
    with session_scope() as session:
        labels = _labels(session, live, cpcb_index(session, now))
        last_hour = _last_hourly(session)

    # Latest 15-minute readings, one call per station.
    latest_rows: list[dict[str, Any]] = []
    newest_pm25: dict[int, openaq.Reading] = {}  # each station's newest PM2.5 reading
    failed: dict[str, str] = {}
    for loc in live:
        try:
            readings, fetch_id = client.latest(loc)
        except SourceUnavailable as exc:  # OpenAQ is paused after repeated failures: stop here
            failed["*"] = str(exc)
            break
        except FetchError as exc:
            failed[str(loc.id)] = str(exc)[:200]
            continue
        latest_rows += [_reading_row(r, loc, labels[loc.id], fetch_id) for r in readings]
        for reading in readings:
            if reading.parameter == "pm25":
                newest_pm25[loc.id] = reading
    with session_scope() as session:
        stored_latest = upsert_obs(session, latest_rows)

    # Hourly PM2.5 means: a week for a new station; afterwards only once its latest reading shows
    # that another hour has been completed since the newest hourly mean we hold.
    due = []
    for loc in live:
        newest = newest_pm25.get(loc.id)
        last = last_hour.get(str(loc.id))
        if newest is None or (last is not None and newest.observed_at < last + timedelta(hours=1)):
            continue
        week_back = last is None or last - REREAD < now - BACKFILL
        due.append((hourly_since(last, now), loc, newest.sensor_id, week_back))
    due.sort(key=lambda item: item[0])  # the station furthest behind first
    hourly_rows: list[dict[str, Any]] = []
    backfilled = 0
    for since, loc, sensor, week_back in due[:MAX_HOURLY_CALLS]:
        if "*" in failed:
            break
        try:
            readings, fetch_id = client.hourly(
                location_id=loc.id, sensor_id=sensor, parameter="pm25", since=since, until=now
            )
        except SourceUnavailable as exc:
            failed["*"] = str(exc)
            break
        except FetchError as exc:
            failed[f"{loc.id}/hourly"] = str(exc)[:200]
            continue
        if week_back:
            backfilled += 1
        hourly_rows += [_reading_row(r, loc, labels[loc.id], fetch_id) for r in readings]
    with session_scope() as session:
        stored_hourly = upsert_obs(session, hourly_rows)

    if live and not latest_rows and failed:
        raise RuntimeError(f"OpenAQ: every station failed ({next(iter(failed.values()))})")
    return {
        "stations_listed": len(listed),
        "stations_live": len(live),
        "latest_rows": stored_latest,
        "hourly_rows": stored_hourly,
        "hourly_calls": min(len(due), MAX_HOURLY_CALLS),
        "hourly_backfilled_stations": backfilled,
        "hourly_left_for_next_run": max(len(due) - MAX_HOURLY_CALLS, 0),
        "unlabelled_stations": sorted(loc.name for loc in live if labels[loc.id][0] is None),
        "calls": client.calls,
        "failed": failed,
    }


# --- step 3: CAMS -------------------------------------------------------------------------------


def city_points(session: Session, now: datetime) -> list[tuple[str, str | None, float, float]]:
    """(city, state, lat, lon) for every city with a station reporting in the last week; the
    position is the median of its stations' coordinates."""
    rows = session.execute(
        text(
            """
            WITH stations AS (
                SELECT DISTINCT ON (provider, station_id) city, state, lat, lon
                FROM air_obs
                WHERE NOT is_model AND city IS NOT NULL AND observed_at > :since
                ORDER BY provider, station_id, observed_at DESC
            )
            SELECT city, mode() WITHIN GROUP (ORDER BY state) AS state,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS lat,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY lon) AS lon
            FROM stations GROUP BY city ORDER BY city
            """
        ),
        {"since": now - BACKFILL},
    )
    return [(r.city, r.state, round(r.lat, 4), round(r.lon, 4)) for r in rows]


def _stored_cams_run(session: Session) -> dict[str, Any] | None:
    """The meta of the newest CAMS data fetch whose rows were stored (None if there is none).

    A fetch counts once cams_step has marked it stored. Rows alone are not enough: a refetch of
    the same run whose values equal the stored ones updates no row, so it would never be found,
    and every 30-min run would fetch again until the next model run. (Fetches stored before the
    mark existed are recognised by their rows.)"""
    return session.execute(
        text(
            """
            SELECT f.meta FROM source_fetch f
            WHERE f.source_key = :key AND f.ok AND f.meta->>'kind' = 'air_quality'
              AND (f.meta->>'stored' = 'true'
                   OR EXISTS (SELECT 1 FROM air_obs o WHERE o.fetch_id = f.id))
            ORDER BY f.started_at DESC LIMIT 1
            """
        ),
        {"key": CAMS},
    ).scalar()


def cams_step(now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    run, _meta_fetch = cams.latest_run()
    with session_scope() as session:
        points = city_points(session, now)
        stored = _stored_cams_run(session)
    if not points:
        return {"skipped": "no city has a station yet, so there is nowhere to sample the model"}
    digest = hashlib.sha256(repr(points).encode()).hexdigest()[:16]
    run_meta = {
        "run_initialised": iso_z(run.initialised),
        "run_available": iso_z(run.available),
        "points": digest,
        "cities": len(points),
    }
    if (
        stored
        and stored.get("run_available") == run_meta["run_available"]
        and stored.get("points") == digest
    ):
        return {"skipped": "this CAMS run is already stored", **run_meta}
    state_of = {city: state for city, state, _lat, _lon in points}
    series, result = cams.fetch_series(
        [cams.Point(city, lat, lon) for city, _state, lat, lon in points], meta=run_meta
    )
    rows = [
        _obs(
            provider=CAMS,
            station_id=s.name,
            station_name=f"CAMS model, {s.name}",
            city=s.name,
            state=state_of.get(s.name),
            lat=s.lat,
            lon=s.lon,
            parameter=parameter,
            value=float(value),
            unit=UGM3,
            observed_at=t,
            period_minutes=60,  # hourly model steps (instantaneous values, 0.4° cells)
            is_model=True,
            fetch_id=result.fetch_id,
        )
        for s in series
        for parameter, values in s.values.items()
        for t, value in zip(s.times, values, strict=True)
        if value is not None
    ]
    with session_scope() as session:
        stored_rows = upsert_obs(session, rows)
    set_fetch_records(result.fetch_id, len(rows), {"stored": True})  # see _stored_cams_run
    return {
        "rows": stored_rows,
        **run_meta,
        "valid_from": iso_z(min((s.times[0] for s in series if s.times), default=None)),
        "valid_to": iso_z(max((s.times[-1] for s in series if s.times), default=None)),
    }


# --- the jobs -----------------------------------------------------------------------------------


@job("air", every_minutes=30)
def air() -> dict[str, Any]:
    """CPCB stations (data.gov.in sub-indices, OpenAQ µg/m³) + CAMS model at cities -> air_obs."""
    summary: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, step in (("datagov", datagov_step), ("openaq", openaq_step), ("cams", cams_step)):
        try:
            summary[name] = step()
        except Exception as exc:  # one source down must not stop the others
            log.exception("air: %s step failed", name)
            errors[name] = get_settings().redact(f"{type(exc).__name__}: {exc}")[:300]
    if errors:
        summary["errors"] = errors
    if len(errors) == 3:
        raise RuntimeError(f"every air-quality source failed: {errors}")
    return summary


@job("air_dss", cron="20 10,14,20 * * *")
def air_dss() -> dict[str, Any]:
    """IITM DSS: share of Delhi's PM2.5 attributed to stubble burning -> dss_snapshot."""
    data, result = iitm_dss.fetch_dss()
    now = utcnow()
    digest = data.sha256()
    with session_scope() as session:
        known = session.scalar(select(DssSnapshot.id).where(DssSnapshot.data_sha256 == digest))
        stmt = pg_insert(DssSnapshot).values(
            data_sha256=digest,
            run_date=data.run_date,
            daily_pct=data.daily_pct,
            forecast_pct=data.forecast_pct,
            basis=data.basis,
            first_seen_at=now,
            last_seen_at=now,
            fetch_id=result.fetch_id,
        )
        # A version seen again keeps first_seen_at and now cites this newest read, so the
        # response's sources[].fetched_at says when the numbers were last confirmed.
        seen_again: dict[str, Any] = {"last_seen_at": now}
        if result.fetch_id is not None:
            seen_again["fetch_id"] = result.fetch_id
        session.execute(
            stmt.on_conflict_do_update(index_elements=["data_sha256"], set_=seen_again)
        )
    today = now.astimezone(IST).date()
    day0 = data.forecast_pct.get(data.run_date.isoformat(), {})
    return {
        "run_date": data.run_date.isoformat(),
        "stale": data.run_date < today,
        "stubble_pct_run_date": day0.get(iitm_dss.STUBBLE),
        "days_in_season_series": len(data.daily_pct),
        "new_version": known is None,
    }
