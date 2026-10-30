"""Job `tle`: CelesTrak orbital elements for the eight fire and imaging satellites -> tle.

Runs every 12 h (architecture.md section 6). It obeys CelesTrak's usage policy in code, because a
breach gets the IP firewalled for everyone on it:
- never sooner than 2 h after the last successful download (CelesTrak updates every 2 h and asks
  for one download per update), even when someone runs the job by hand;
- one small query per satellite, 2 s apart;
- stop at the first non-200 answer instead of trying the other satellites.
Elements stay valid for days, so a skipped or failed run just leaves the last good set in place;
/api/orbits/tle shows its age.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from blindspot.db import session_scope
from blindspot.http import FetchError, set_fetch_records
from blindspot.jobs import job
from blindspot.models.orbits import Tle
from blindspot.provenance import iso_z, utcnow
from blindspot.sources.celestrak import MIN_REFRESH, SATELLITES, TleFormatError, fetch_tle

log = logging.getLogger(__name__)

PAUSE_S = 2.0  # between CelesTrak queries, to stay a gentle client


def last_celestrak_download() -> datetime | None:
    """When CelesTrak last answered us successfully (from source_fetch), or None."""
    with session_scope() as session:
        return session.execute(
            text("SELECT max(finished_at) FROM source_fetch WHERE source_key = 'celestrak' AND ok")
        ).scalar()


def refresh_allowed(last_download: datetime | None, now: datetime) -> bool:
    """CelesTrak publishes every 2 h and asks for one download per update."""
    return last_download is None or now - last_download >= MIN_REFRESH


def _upsert(record: Any, sat: Any, fetch_id: int | None, fetched_at: datetime) -> None:
    values = {
        "norad_id": record.norad_id,
        "key": sat.key,
        "name": sat.name,
        "object_name": record.object_name,
        "instrument": sat.instrument,
        "line1": record.line1,
        "line2": record.line2,
        "epoch": record.epoch,
        "fetched_at": fetched_at,
        "fetch_id": fetch_id,
    }
    stmt = insert(Tle).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Tle.norad_id],
        set_={k: stmt.excluded[k] for k in values if k != "norad_id"},
        where=Tle.epoch <= stmt.excluded.epoch,  # an older element set never replaces a newer one
    )
    with session_scope() as session:
        session.execute(stmt)


@job("tle", every_minutes=12 * 60)
def tle() -> dict[str, Any]:
    """CelesTrak GP elements for Suomi NPP, NOAA-20/21, Aqua, Terra and Sentinel-2A/B/C -> tle."""
    now = utcnow()
    last = last_celestrak_download()
    if not refresh_allowed(last, now):
        minutes = (now - last).total_seconds() / 60 if last else 0
        return {
            "skipped": f"last CelesTrak download {minutes:.0f} min ago; CelesTrak publishes new "
            f"elements every {MIN_REFRESH.total_seconds() / 3600:.0f} h and asks for one "
            "download per update",
        }

    epochs: dict[str, str | None] = {}
    errors: list[str] = []
    for i, sat in enumerate(SATELLITES):
        if i:
            time.sleep(PAUSE_S)
        try:
            record, result = fetch_tle(sat)
        except FetchError as exc:
            # Policy: stop at the first non-200 and let a human look (the failed run says why).
            errors.append(f"{sat.name}: {exc}; stopped, as CelesTrak asks of automated clients")
            break
        except TleFormatError as exc:  # a 200 that is not this satellite's TLE: skip it, go on
            errors.append(str(exc))
            continue
        _upsert(record, sat, result.fetch_id, result.fetched_at or utcnow())
        set_fetch_records(result.fetch_id, 1, {"epoch": iso_z(record.epoch)})
        epochs[sat.name] = iso_z(record.epoch)

    if errors:
        stored = ", ".join(epochs) or "none"
        raise RuntimeError(f"tle: {len(errors)} problem(s): {' | '.join(errors)} (stored: {stored})")
    return {"stored": len(epochs), "epochs": epochs}
