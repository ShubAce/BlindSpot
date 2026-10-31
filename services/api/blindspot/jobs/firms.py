"""Jobs that keep fire data current: NASA FIRMS live and season history, and CREAMS official counts.

firms_nrt (every 30 min)
    The keyless South Asia CSVs of the four NRT products (VIIRS on NOAA-20, NOAA-21 and Suomi NPP;
    MODIS on Aqua and Terra). HEAD each file first and download it only when its ETag changed
    (FIRMS ignores conditional GETs). It picks the smallest file (24h, 48h or 7d) that still
    overlaps the last successful sync, so an outage of up to a week heals itself.
firms_backfill (daily, 12:00 IST)
    The MAP_KEY area API for every UTC day of the season (from 15 Sep) not yet held complete,
    in windows of at most 5 days (the API's limit), under half the key's 10-minute transaction
    allowance. A day becomes complete once fetched a full day after it ended.
creams_daily (every 60 min)
    The CREAMS (ICAR-IARI) official daily layers for every season day not yet final; a day is
    replaced as a whole each time, so corrections flow through.

All three store rows clipped to the AOI box only as a pre-filter; counts are clipped to Indian
state polygons through the state labels written by label_admin() (SYNTHESIS rule). After each
FIRMS ingest, refresh_static_sites() recomputes the persistent heat sources (fire_static_site)
that crop-fire counts leave out.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from blindspot.analytics.fires_stats import (
    STATIC_RADIUS_KM,
    find_static_sites,
    night_of,
    season_start,
)
from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.geo import IST, admin_for_points, point_ewkt
from blindspot.http import FetchError, set_fetch_records
from blindspot.jobs import job
from blindspot.models.fires import (
    FireCoverage,
    FireDetection,
    OfficialFireEvent,
    OfficialLayer,
)
from blindspot.provenance import iso_z, utcnow
from blindspot.sources import creams
from blindspot.sources.firms import (
    MAX_DAY_RANGE,
    PRODUCTS,
    TRANSACTIONS_PER_DAY,
    FireRow,
    KeyStatus,
    Product,
    clip_to_bbox,
    data_availability,
    get_area,
    get_file,
    head_file,
    key_status,
    parse_csv,
)

log = logging.getLogger(__name__)

BATCH = 1000  # rows per INSERT statement
COMPLETE_AFTER = timedelta(days=1)  # a UTC day is complete once fetched this long after it ended
AREA_PAUSE_S = 1.0  # between area-API calls
KEY_SHARE = 0.5  # use at most half the key's 10-minute allowance, leaving room for others
CREAMS_FINAL_AFTER = timedelta(days=1)  # an IST day's layer is final this long after the day
CREAMS_MISSING_FINAL_AFTER = timedelta(days=7)  # stop asking for a layer CREAMS never published


# --- storing -------------------------------------------------------------------------------------


def upsert_detections(session: Session, rows: Iterable[FireRow], fetch_id: int | None) -> int:
    """Insert detections we do not have yet; return how many were new. Idempotent: the unique key
    (source, acq_utc, lat, lon) skips a detection already stored from either feed."""
    unique = {(r.source, r.acq_utc, r.lat, r.lon): r for r in rows}
    inserted = 0
    for chunk in batched(unique.values(), BATCH):
        values = [
            {**r.as_db_row(), "geom": point_ewkt(r.lon, r.lat), "fetch_id": fetch_id}
            for r in chunk
        ]
        stmt = (
            insert(FireDetection)
            .values(values)
            .on_conflict_do_nothing(constraint="uq_fire_detection_key")
            .returning(FireDetection.id)
        )
        inserted += len(session.execute(stmt).all())
    return inserted


def boundary_version(session: Session) -> str | None:
    """A fingerprint of the loaded state/district polygons, or None when there are none. Rows are
    labelled once per fingerprint, so a reload of the boundaries relabels everything."""
    n, max_id = session.execute(
        text(
            "SELECT count(*), coalesce(max(id), 0) FROM boundary "
            "WHERE level IN ('state', 'district')"
        )
    ).one()
    return f"{n}:{max_id}" if n else None


def label_admin(session: Session, table: str) -> int:
    """Write state and district on rows of `table` (fire_detection or official_fire_event) not yet
    labelled with the current boundaries, using blindspot.geo.admin_for_points. Rows outside every
    polygon (Pakistan, other states) keep null names but are not looked up again."""
    if table not in ("fire_detection", "official_fire_event"):
        raise ValueError(f"cannot label {table}")
    version = boundary_version(session)
    if version is None:
        return 0
    labelled = 0
    while True:
        todo = session.execute(
            text(
                f"SELECT id, lon, lat FROM {table} "
                "WHERE admin_version IS DISTINCT FROM :v ORDER BY id LIMIT 5000"
            ),
            {"v": version},
        ).all()
        if not todo:
            return labelled
        areas = admin_for_points(session, [(r.lon, r.lat) for r in todo])
        session.execute(
            text(
                f"""
                UPDATE {table} AS t
                SET state = u.state, district = u.district, admin_version = :v
                FROM unnest(CAST(:ids AS bigint[]), CAST(:states AS text[]),
                            CAST(:districts AS text[])) AS u(id, state, district)
                WHERE t.id = u.id
                """
            ),
            {
                "v": version,
                "ids": [r.id for r in todo],
                "states": [a.state if a else None for a in areas],
                "districts": [a.district if a else None for a in areas],
            },
        )
        labelled += sum(1 for a in areas if a is not None)


def refresh_static_sites(session: Session) -> int:
    """Recompute the persistent heat sources from every stored night detection and replace the
    fire_static_site table (a few hundred rows; idempotent). Returns the number of sites."""
    rows = session.execute(
        text("SELECT lat, lon, acq_utc FROM fire_detection WHERE daynight = 'N'")
    ).all()
    sites = find_static_sites([(r.lat, r.lon, night_of(r.acq_utc)) for r in rows])
    session.execute(text("DELETE FROM fire_static_site"))
    now = utcnow()
    areas = admin_for_points(session, [(s.lon, s.lat) for s in sites])
    for site, admin in zip(sites, areas, strict=True):
        session.execute(
            text(
                """
                INSERT INTO fire_static_site (area, lat, lon, nights, night_detections,
                    first_night, last_night, state, district, computed_at)
                SELECT ST_Multi(ST_Union(ST_Buffer(
                           ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography, :r
                       )::geometry)),
                       :lat, :lon, :nights, :n, :first, :last, :state, :district, :now
                FROM unnest(CAST(:lats AS float8[]), CAST(:lons AS float8[])) AS p(lat, lon)
                """
            ),
            {
                "r": STATIC_RADIUS_KM * 1000,
                "lat": site.lat,
                "lon": site.lon,
                "nights": site.nights,
                "n": site.night_detections,
                "first": site.first_night,
                "last": site.last_night,
                "state": admin.state if admin else None,
                "district": admin.district if admin else None,
                "now": now,
                "lats": [m[0] for m in site.members],
                "lons": [m[1] for m in site.members],
            },
        )
    return len(sites)


# --- firms_nrt -----------------------------------------------------------------------------------


def _last_sync(session: Session, product: Product) -> datetime | None:
    """When this product was last confirmed current (downloaded and stored, or found unchanged)."""
    return session.execute(
        text(
            """
            SELECT max(finished_at) FROM source_fetch
            WHERE source_key = 'firms' AND ok AND meta->>'product' = :p
              AND (meta->>'ingested' = 'true' OR meta->>'unchanged' = 'true')
            """
        ),
        {"p": product.source},
    ).scalar()


def _ingested_etag(session: Session, product: Product, window: str) -> str | None:
    """ETag of the last download of this product's `window` file whose rows were stored, whether
    it came from FIRMS or through the relay (the same bytes; the URLs differ)."""
    return session.execute(
        text(
            """
            SELECT meta->>'etag' FROM source_fetch
            WHERE source_key = 'firms' AND ok AND meta->>'ingested' = 'true'
              AND meta->>'product' = :product AND meta->>'window' = :window
            ORDER BY started_at DESC LIMIT 1
            """
        ),
        {"product": product.source, "window": window},
    ).scalar()


def choose_window(last_sync: datetime | None, now: datetime) -> str:
    """The smallest keyless file that still overlaps our last sync. The 24h file reaches back
    24-27 h from FIRMS's newest pass, which itself lags 1-3 h, hence the 12 h / 36 h steps."""
    if last_sync is not None and now - last_sync <= timedelta(hours=12):
        return "24h"
    if last_sync is not None and now - last_sync <= timedelta(hours=36):
        return "48h"
    return "7d"


def _sync_product(product: Product, now: datetime) -> dict[str, Any]:
    bbox = get_settings().aoi_bbox
    with session_scope() as session:
        window = choose_window(_last_sync(session, product), now)
        known_etag = _ingested_etag(session, product, window)
    head = head_file(product, window)
    etag = head.headers.get("etag")
    if etag and etag == known_etag:
        set_fetch_records(head.fetch_id, 0, {"etag": etag, "unchanged": True})
        return {"window": window, "unchanged": True}

    result = get_file(product, window)
    etag = result.headers.get("etag") or etag
    parsed = parse_csv(result.data, product)
    rows = clip_to_bbox(parsed.rows, bbox)
    with session_scope() as session:
        inserted = upsert_detections(session, rows, result.fetch_id)
    newest = {sat: iso_z(t) for sat, t in parsed.newest_by_satellite.items()}
    set_fetch_records(
        result.fetch_id,
        len(rows),
        {
            "etag": etag,
            "ingested": True,
            "rows_file": len(parsed.rows),
            "rows_aoi_box": len(rows),
            "inserted": inserted,
            "skipped": dict(parsed.skipped),
            "newest_by_satellite": newest,
        },
    )
    if window != "24h":
        log.info("firms_nrt %s: caught up with the %s file", product.source, window)
    return {
        "window": window,
        "rows_file": len(parsed.rows),
        "rows_aoi_box": len(rows),
        "inserted": inserted,
        "skipped": dict(parsed.skipped),
        "newest": newest,
    }


@job("firms_nrt", every_minutes=30)
def firms_nrt() -> dict[str, Any]:
    """Keyless FIRMS NRT CSVs (VIIRS NOAA-20/21, Suomi NPP; MODIS) -> fire_detection + labels."""
    now = utcnow()
    summary: dict[str, Any] = {}
    failures = 0
    for product in PRODUCTS:
        try:
            summary[product.source] = _sync_product(product, now)
        except (FetchError, ValueError) as exc:  # one product failing must not stop the others
            failures += 1
            summary[product.source] = {"error": str(exc)[:300]}
            log.warning("firms_nrt %s failed: %s", product.source, exc)
    with session_scope() as session:
        summary["labelled"] = label_admin(session, "fire_detection")
        summary["static_sites"] = refresh_static_sites(session)
    if failures == len(PRODUCTS):
        raise RuntimeError(f"every FIRMS product failed: {summary}")
    return summary


# --- firms_backfill ------------------------------------------------------------------------------


class TransactionBudget:
    """Keeps this run's area-API use under a share of the key's 10-minute allowance. FIRMS
    counts 2 transactions per day requested (measured); the key may also serve other callers,
    so their use at the start of the run is counted too."""

    WINDOW_S = 600.0

    def __init__(self, status: KeyStatus, share: float = KEY_SHARE) -> None:
        self.cap = status.limit * share
        self.others = status.used
        self.spent: deque[tuple[float, int]] = deque()

    def spend(self, cost: int) -> float:
        """Wait (if needed) until `cost` fits; return the seconds waited."""
        waited = 0.0
        while True:
            now = time.monotonic()
            while self.spent and now - self.spent[0][0] > self.WINDOW_S:
                self.spent.popleft()
            if self.others + sum(c for _, c in self.spent) + cost <= self.cap:
                self.spent.append((now, cost))
                return waited
            if not self.spent:
                raise RuntimeError(f"FIRMS key busy: {self.others} transactions in use")
            pause = self.WINDOW_S - (now - self.spent[0][0]) + 1.0
            log.info("firms_backfill: waiting %.0f s for the transaction window", pause)
            time.sleep(pause)
            waited += pause


def windows_for(days: Sequence[date], size: int = MAX_DAY_RANGE) -> list[tuple[date, int]]:
    """Group days into (start, length) runs of consecutive days, at most `size` long."""
    out: list[tuple[date, int]] = []
    for day in sorted(days):
        if out and out[-1][0] + timedelta(days=out[-1][1]) == day and out[-1][1] < size:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((day, 1))
    return out


def _mark_coverage(
    session: Session, source: str, start: date, n: int, rows: Sequence[FireRow],
    fetch_id: int | None, fetched_at: datetime,
) -> int:  # fmt: skip
    per_day = Counter(r.acq_utc.date() for r in rows)
    complete = 0
    for i in range(n):
        day = start + timedelta(days=i)
        done = fetched_at >= datetime.combine(day, datetime.min.time(), UTC) + timedelta(days=1) + COMPLETE_AFTER
        complete += done
        values = {"source": source, "day": day, "rows": per_day.get(day, 0), "complete": done,
                  "fetched_at": fetched_at, "fetch_id": fetch_id}  # fmt: skip
        stmt = insert(FireCoverage).values(values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[FireCoverage.source, FireCoverage.day],
                set_={k: stmt.excluded[k] for k in ("rows", "complete", "fetched_at", "fetch_id")},
            )
        )
    return complete


@job("firms_backfill", cron="0 12 * * *")
def firms_backfill() -> dict[str, Any]:
    """FIRMS area API (MAP_KEY): every season day not held complete, <=5-day windows -> fire_detection."""
    key = get_settings().firms_map_key
    if key is None:
        return {"skipped": "FIRMS_MAP_KEY is not set, so the season history cannot be fetched"}
    bbox = get_settings().aoi_bbox
    now = utcnow()
    today = now.date()
    start = season_start(today)
    status = key_status(key)
    if status.used > status.limit * KEY_SHARE:
        return {"skipped": f"FIRMS key busy ({status.used}/{status.limit} transactions in use)"}
    budget = TransactionBudget(status)
    available = data_availability(key)

    summary: dict[str, Any] = {"season_start": start.isoformat(), "key_used_at_start": status.used}
    calls = failures = 0
    for product in PRODUCTS:
        first, last = available.get(product.source, (None, None))
        if first is None or last is None:
            summary[product.source] = {"skipped": "not listed by data_availability"}
            continue
        lo, hi = max(start, first), min(today, last)
        with session_scope() as session:
            done = set(
                session.execute(
                    text(
                        "SELECT day FROM fire_coverage WHERE source = :s AND complete "
                        "AND day BETWEEN :lo AND :hi"
                    ),
                    {"s": product.source, "lo": lo, "hi": hi},
                ).scalars()
            )
        todo = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
        todo = [d for d in todo if d not in done]
        stats: Counter[str] = Counter()
        for window_start, n in windows_for(todo):
            if calls:
                time.sleep(AREA_PAUSE_S)
            budget.spend(n * TRANSACTIONS_PER_DAY)
            calls += 1
            try:
                result = get_area(key, product.source, bbox, n, window_start)
                parsed = parse_csv(result.data, product)
            except (FetchError, ValueError) as exc:  # keep what we have; the next run resumes
                failures += 1
                stats["errors"] += 1
                summary.setdefault("errors", []).append(f"{product.source} {window_start}: {exc}"[:300])
                if isinstance(exc, FetchError) and (exc.status_code or 0) in (400, 401, 403):
                    break  # a rejected key or request will not work for the next window either
                continue
            rows = clip_to_bbox(parsed.rows, bbox)
            fetched_at = result.fetched_at or utcnow()
            with session_scope() as session:
                inserted = upsert_detections(session, rows, result.fetch_id)
                complete = _mark_coverage(
                    session, product.source, window_start, n, rows, result.fetch_id, fetched_at
                )
            set_fetch_records(result.fetch_id, len(rows), {"inserted": inserted,
                                                           "skipped": dict(parsed.skipped)})  # fmt: skip
            stats.update(calls=1, days=n, rows=len(rows), inserted=inserted, complete_days=complete)
        summary[product.source] = {"range": [lo.isoformat(), hi.isoformat()],
                                   "already_complete_days": len(done), **stats}  # fmt: skip
    with session_scope() as session:
        summary["labelled"] = label_admin(session, "fire_detection")
        summary["static_sites"] = refresh_static_sites(session)
    summary["transactions_est"] = sum(c for _, c in budget.spent)
    if failures and failures == calls:
        raise RuntimeError(f"firms_backfill: every area call failed: {summary.get('errors')}")
    return summary


# --- creams_daily --------------------------------------------------------------------------------


def _replace_layer(session: Session, day: date, events: Sequence[creams.OfficialEvent],
                   fetch_id: int | None) -> None:  # fmt: skip
    """Swap a day's events for the freshly fetched set, in one transaction."""
    session.execute(
        text("DELETE FROM official_fire_event WHERE layer = :layer AND layer_date = :d"),
        {"layer": creams.LAYER, "d": day},
    )
    unique = {(e.satellite_raw, e.acq_utc, e.lat, e.lon): e for e in events}
    for chunk in batched(unique.values(), BATCH):
        session.execute(
            insert(OfficialFireEvent)
            .values([{**e.as_db_row(), "geom": point_ewkt(e.lon, e.lat), "fetch_id": fetch_id}
                     for e in chunk])  # fmt: skip
            .on_conflict_do_nothing(constraint="uq_official_fire_event_key")
        )


def _mark_layer(session: Session, day: date, events: int, published: bool, final: bool,
                fetched_at: datetime, fetch_id: int | None) -> None:  # fmt: skip
    values = {"layer": creams.LAYER, "layer_date": day, "events": events, "published": published,
              "final": final, "fetched_at": fetched_at, "fetch_id": fetch_id}  # fmt: skip
    stmt = insert(OfficialLayer).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[OfficialLayer.layer, OfficialLayer.layer_date],
            set_={k: stmt.excluded[k] for k in ("events", "published", "final", "fetched_at",
                                                 "fetch_id")},  # fmt: skip
        )
    )


@job("creams_daily", every_minutes=60)
def creams_daily() -> dict[str, Any]:
    """CREAMS (ICAR-IARI) official residue-burning layers, 15 Sep -> today IST -> official_fire_event."""
    now = utcnow()
    today = now.astimezone(IST).date()
    first = season_start(today)
    with session_scope() as session:
        final = set(
            session.execute(
                text("SELECT layer_date FROM official_layer WHERE layer = :l AND final"),
                {"l": creams.LAYER},
            ).scalars()
        )
    todo = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    todo = [d for d in reversed(todo) if d not in final]  # newest first
    published, _ = creams.available_layers()
    stats: Counter[str] = Counter()
    missing: list[str] = []
    errors: list[str] = []
    for day in todo:
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), IST)
        try:
            if day not in published:
                raise creams.LayerNotPublished(f"no {creams.LAYER} layer for {day} (yet)")
            events, skipped, result = creams.fetch_layer(day)
        except creams.LayerNotPublished:
            with session_scope() as session:
                _mark_layer(session, day, 0, False, now >= day_end + CREAMS_MISSING_FINAL_AFTER,
                            now, None)  # fmt: skip
            missing.append(day.isoformat())
            continue
        except FetchError as exc:
            errors.append(f"{day}: {exc}"[:300])
            if exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500:
                break  # CREAMS is down or paused: stop; the next hourly run tries again
            continue
        except creams.CreamsFormatError as exc:
            errors.append(f"{day}: {exc}"[:300])
            continue
        with session_scope() as session:
            _replace_layer(session, day, events, result.fetch_id)
            _mark_layer(session, day, len(events), True, now >= day_end + CREAMS_FINAL_AFTER,
                        result.fetched_at or now, result.fetch_id)  # fmt: skip
        set_fetch_records(result.fetch_id, len(events), {"skipped": skipped})
        stats.update(layers=1, events=len(events), skipped=skipped)
    with session_scope() as session:
        stats["labelled"] = label_admin(session, "official_fire_event")
    if errors and not stats.get("layers"):
        raise RuntimeError(f"creams_daily: no layer fetched; {' | '.join(errors[:3])}")
    return {"season_start": first.isoformat(), "days_checked": len(todo), **stats,
            "not_published": missing[:10], "errors": errors[:5]}  # fmt: skip
