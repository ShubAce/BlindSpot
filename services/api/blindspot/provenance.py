"""Where every number comes from.

- SOURCE_REGISTRY: the upstream sources (label, kind, how often a job refreshes them, docs link).
- record_fetch() / track_fetch(): one source_fetch row per upstream call. blindspot.http.fetch does
  this for you; wrap SDK calls (pystac-client, google-genai, rasterio reads) in track_fetch().
- SourceRef, ref_for_fetch(), latest_refs(): what API responses list under `sources`.
- source_status(): ok / stale / error / never for each source, for GET /api/sources.

Every timestamp is UTC. utcnow() and iso_z() live here because every SourceRef needs them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.config import get_settings
from blindspot.db import session_scope

# SourceFetch is imported inside the functions that use it, not up here. Importing
# blindspot.models runs every model file, and a model file may import this module (for utcnow or
# SourceRef): a top-level import would hand that file a half-loaded module, and its table would
# silently go missing (a circular import).
if TYPE_CHECKING:
    from blindspot.models.core import SourceFetch

log = logging.getLogger(__name__)

STALE_AFTER_CADENCES = 3  # stale when the last success is older than 3 x the source's cadence


def utcnow() -> datetime:
    """The current time as an aware UTC datetime."""
    return datetime.now(UTC)


def iso_z(value: datetime | None) -> str | None:
    """ISO-8601 in UTC with a Z suffix, the format of every time in the API. Naive means UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


# --- registry -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceInfo:
    key: str
    label: str
    kind: str  # fires / orbits / air / air_model / weather / imagery / landcover / boundaries / ...
    cadence_minutes: int | None  # how often a job refreshes it; None = on demand or loaded once
    docs_url: str | None


SOURCE_REGISTRY: dict[str, SourceInfo] = {}


def register_source(info: SourceInfo) -> SourceInfo:
    """Add or refine a source. Modules call this from their own file instead of editing this one
    (for example, the boundaries module naming the exact dataset and docs link it loads)."""
    SOURCE_REGISTRY[info.key] = info
    return info


# The hourly job group (fires, air, GK-2A) runs hourly in the afternoon and every 3 h otherwise
# (data-jobs.yml), and the CDN keeps answers until :30: its sources declare 80 minutes, so 3 x
# cadence is the headline's 4-hour rule and fresh data is never called stale between runs.
HOURLY_GROUP_CADENCE = 80

for _info in (
    SourceInfo(
        "firms", "NASA FIRMS active fires", "fires", HOURLY_GROUP_CADENCE, "https://firms.modaps.eosdis.nasa.gov/api/"
    ),
    SourceInfo(
        "celestrak",
        "CelesTrak GP orbital elements",
        "orbits",
        12 * 60,
        "https://celestrak.org/NORAD/documentation/gp-data-formats.php",
    ),
    SourceInfo("openaq", "OpenAQ v3 (CPCB stations)", "air", HOURLY_GROUP_CADENCE, "https://docs.openaq.org/"),
    SourceInfo(
        "datagov_aqi",
        "data.gov.in CPCB real-time AQI",
        "air",
        HOURLY_GROUP_CADENCE,
        "https://data.gov.in/resource/real-time-air-quality-index-various-locations",
    ),
    SourceInfo(
        "cams",
        "Copernicus CAMS via Open-Meteo air quality",
        "air_model",
        HOURLY_GROUP_CADENCE,
        "https://open-meteo.com/en/docs/air-quality-api",
    ),
    SourceInfo(
        "openmeteo",
        "Open-Meteo forecast (winds, boundary layer)",
        "weather",
        3 * 60,
        "https://open-meteo.com/en/docs",
    ),
    SourceInfo(
        "stac_pc",
        "Microsoft Planetary Computer STAC: Sentinel-2 L2A",
        "imagery",
        6 * 60,
        "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a",
    ),
    SourceInfo(
        "stac_e84",
        "Element 84 Earth Search STAC",
        "imagery",
        6 * 60,
        "https://element84.com/earth-search/",
    ),
    # Registered here as well as in sources/stac.py, which the API never imports: burn-scar answers
    # cite it, and without a label the investigator showed the raw key "s2_cog".
    SourceInfo(
        "s2_cog",
        "Sentinel-2 L2A pixels (Earth Search COGs on AWS)",
        "imagery",
        6 * 60,
        "https://registry.opendata.aws/sentinel-2-l2a-cogs/",
    ),
    SourceInfo(
        "pc_data",
        "Planetary Computer data/tiler API",
        "imagery",
        None,
        "https://planetarycomputer.microsoft.com/api/data/v1/docs",
    ),
    SourceInfo("worldcover", "ESA WorldCover", "landcover", None, "https://esa-worldcover.org/en"),
    # The boundaries module names the exact dataset (and its docs link) with register_source().
    SourceInfo("boundaries", "Administrative boundaries", "boundaries", None, None),
    SourceInfo(
        "osm",
        "OpenStreetMap Overpass API",
        "places",
        None,
        "https://wiki.openstreetmap.org/wiki/Overpass_API",
    ),
    SourceInfo("gemini", "Google Gemini API", "llm", 24 * 60, "https://ai.google.dev/gemini-api/docs"),
):
    register_source(_info)


def label_for(key: str) -> str:
    info = SOURCE_REGISTRY.get(key)
    return info.label if info else key


# --- recording fetches --------------------------------------------------------------------------

_warned_unregistered: set[str] = set()


def record_fetch(
    source_key: str,
    url: str | None,
    *,
    ok: bool,
    started_at: datetime,
    finished_at: datetime | None = None,
    http_status: int | None = None,
    records: int | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int | None:
    """Write one source_fetch row in its own transaction and return its id.

    The row commits independently of the caller, so a job that later fails still leaves a record
    of what it fetched. Recording never breaks the caller: on a database error it logs and
    returns None. URL, error and meta are passed through Settings.redact() as a last line of
    defence.
    """
    from blindspot.models.core import SourceFetch  # see the note at the top of this file

    if source_key not in SOURCE_REGISTRY and source_key not in _warned_unregistered:
        _warned_unregistered.add(source_key)
        log.warning("source %r is not in SOURCE_REGISTRY; see register_source()", source_key)
    settings = get_settings()
    try:
        row = SourceFetch(
            source_key=source_key,
            url=settings.redact(url) if url else None,
            started_at=started_at,
            finished_at=finished_at or utcnow(),
            ok=ok,
            http_status=http_status,
            records=records,
            latency_ms=latency_ms,
            error=settings.redact(error)[:2000] if error else None,
            meta=_redact_meta(meta),
        )
        with session_scope() as session:
            session.add(row)
            session.flush()
            fetch_id = row.id
    except Exception:
        log.warning("could not record a %s fetch in source_fetch", source_key, exc_info=True)
        return None
    return fetch_id


def set_fetch_records(fetch_id: int | None, n: int, meta: dict[str, Any] | None = None) -> None:
    """Store how many records a fetch produced once the caller has parsed it, and merge `meta`
    into the row's meta. Does nothing for fetch_id None (a fetch that could not be recorded)."""
    from blindspot.models.core import SourceFetch  # see the note at the top of this file

    if fetch_id is None:
        return
    try:
        with session_scope() as session:
            row = session.get(SourceFetch, fetch_id)
            if row is None:
                log.warning("set_fetch_records: no source_fetch row %s", fetch_id)
                return
            row.records = n
            if meta:
                row.meta = {**(row.meta or {}), **(_redact_meta(meta) or {})}
    except Exception:
        log.warning("could not update records for fetch %s", fetch_id, exc_info=True)


def _redact_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """`meta` with every configured key masked, wherever it sits (say, a copy of the params)."""
    if not meta:
        return None
    options = orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_UTC_Z
    raw = orjson.dumps(meta, option=options, default=str).decode()
    redacted = get_settings().redact(raw)
    return meta if redacted == raw else orjson.loads(redacted)


@dataclass
class FetchTracker:
    """What the code inside a track_fetch() block fills in; fetch_id is set when the block ends."""

    source_key: str
    url: str | None = None
    records: int | None = None
    http_status: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    fetch_id: int | None = None


@contextmanager
def track_fetch(
    source_key: str, url: str | None = None, *, meta: dict[str, Any] | None = None
) -> Iterator[FetchTracker]:
    """Record an upstream call made without blindspot.http.fetch (an SDK, a raster read).

    Times the block and writes one source_fetch row: ok when the block finishes, the error text
    when it raises (the exception still propagates).

        with track_fetch("stac_pc", url=search_url) as tracker:
            items = list(search.items())
            tracker.records = len(items)
        ref = ref_for_fetch(session, tracker.fetch_id)
    """
    tracker = FetchTracker(source_key=source_key, url=url, meta=dict(meta or {}))
    started_at = utcnow()
    t0 = time.perf_counter()
    try:
        yield tracker
    except Exception as exc:
        tracker.fetch_id = record_fetch(
            source_key,
            tracker.url,
            ok=False,
            started_at=started_at,
            http_status=tracker.http_status,
            latency_ms=_ms_since(t0),
            error=f"{type(exc).__name__}: {exc}",
            meta=tracker.meta,
        )
        raise
    tracker.fetch_id = record_fetch(
        source_key,
        tracker.url,
        ok=True,
        started_at=started_at,
        http_status=tracker.http_status,
        records=tracker.records,
        latency_ms=_ms_since(t0),
        meta=tracker.meta,
    )


# --- citing fetches -----------------------------------------------------------------------------


class SourceRef(BaseModel):
    """One entry of a response's `sources` list: which upstream, which fetch, and when."""

    key: str
    label: str
    url: str | None = None
    fetched_at: datetime | None = None
    fetch_id: int | None = None


def _ref(row: SourceFetch) -> SourceRef:
    return SourceRef(
        key=row.source_key,
        label=label_for(row.source_key),
        url=row.url,
        fetched_at=row.finished_at or row.started_at,
        fetch_id=row.id,
    )


def ref_for_fetch(session: Session, fetch_id: int | None) -> SourceRef | None:
    """The SourceRef for one recorded fetch (e.g. FetchResult.fetch_id), or None."""
    from blindspot.models.core import SourceFetch  # see the note at the top of this file

    if fetch_id is None:
        return None
    row = session.get(SourceFetch, fetch_id)
    return _ref(row) if row is not None else None


def latest_refs(session: Session, keys: Iterable[str]) -> list[SourceRef]:
    """The latest successful fetch of each source, in the order given.

    A source never fetched successfully still appears, with url, fetched_at and fetch_id null,
    so the UI can say "no data yet" instead of hiding the source.
    """
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return []
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (source_key) id, source_key, url, started_at, finished_at
            FROM source_fetch
            WHERE ok AND source_key = ANY(:keys)
            ORDER BY source_key, started_at DESC
            """
        ),
        {"keys": wanted},
    ).mappings()
    found = {
        r["source_key"]: SourceRef(
            key=r["source_key"],
            label=label_for(r["source_key"]),
            url=r["url"],
            fetched_at=r["finished_at"] or r["started_at"],
            fetch_id=r["id"],
        )
        for r in rows
    }
    return [found.get(key) or SourceRef(key=key, label=label_for(key)) for key in wanted]


# --- status for GET /api/sources ----------------------------------------------------------------

# For each source: its latest attempt, latest success and latest failure (three index lookups).
_STATUS_SQL = text(
    """
    SELECT k.key,
           la.ok                                     AS last_attempt_ok,
           COALESCE(la.finished_at, la.started_at)   AS last_attempt_at,
           ls.id                                     AS success_id,
           ls.url                                    AS success_url,
           COALESCE(ls.finished_at, ls.started_at)   AS last_success_at,
           ls.records                                AS records_last,
           ls.latency_ms                             AS latency_ms_last,
           COALESCE(lf.finished_at, lf.started_at)   AS last_failure_at,
           lf.error                                  AS last_failure_error
    FROM unnest(CAST(:keys AS text[])) WITH ORDINALITY AS k(key, i)
    LEFT JOIN LATERAL (
        SELECT ok, started_at, finished_at FROM source_fetch
        WHERE source_key = k.key ORDER BY started_at DESC LIMIT 1
    ) la ON true
    LEFT JOIN LATERAL (
        SELECT id, url, started_at, finished_at, records, latency_ms FROM source_fetch
        WHERE source_key = k.key AND ok ORDER BY started_at DESC LIMIT 1
    ) ls ON true
    LEFT JOIN LATERAL (
        SELECT started_at, finished_at, error FROM source_fetch
        WHERE source_key = k.key AND NOT ok ORDER BY started_at DESC LIMIT 1
    ) lf ON true
    ORDER BY k.i
    """
)


def _status(
    info: SourceInfo, last_attempt_ok: bool | None, last_success_at: datetime | None, now: datetime
) -> str:
    """never: no attempt yet.
    Sources with a cadence: ok while the last success is younger than 3 x cadence; after that
    error if the latest attempt failed, stale if it worked (the job has stopped running).
    On-demand sources (no cadence): ok or error from the latest attempt."""
    if last_attempt_ok is None:
        return "never"
    if info.cadence_minutes is None:
        return "ok" if last_attempt_ok else "error"
    max_age = timedelta(minutes=STALE_AFTER_CADENCES * info.cadence_minutes)
    if last_success_at is not None and now - last_success_at <= max_age:
        return "ok"
    return "stale" if last_attempt_ok else "error"


def source_status(session: Session) -> list[dict[str, Any]]:
    """One entry per source: the /api/sources fields, plus url / fetched_at / fetch_id of the last
    success so that each entry is also a valid SourceRef. Sources found in source_fetch but missing
    from the registry are listed too (kind "unregistered") rather than hidden."""
    keys = list(SOURCE_REGISTRY)
    seen = session.execute(text("SELECT DISTINCT source_key FROM source_fetch")).scalars()
    keys += sorted(k for k in seen if k not in SOURCE_REGISTRY)
    now = utcnow()
    out = []
    for r in session.execute(_STATUS_SQL, {"keys": keys}).mappings():
        info = SOURCE_REGISTRY.get(r["key"]) or SourceInfo(
            r["key"], r["key"], "unregistered", None, None
        )
        success_at, failure_at = r["last_success_at"], r["last_failure_at"]
        unresolved = failure_at is not None and (success_at is None or failure_at > success_at)
        out.append(
            {
                "key": info.key,
                "label": info.label,
                "kind": info.kind,
                "status": _status(info, r["last_attempt_ok"], success_at, now),
                "last_success_at": success_at,
                "last_attempt_at": r["last_attempt_at"],
                "last_error": r["last_failure_error"] if unresolved else None,
                "records_last": r["records_last"],
                "latency_ms_last": r["latency_ms_last"],
                "cadence_minutes": info.cadence_minutes,
                "docs_url": info.docs_url,
                "url": r["success_url"],
                "fetched_at": success_at,
                "fetch_id": r["success_id"],
            }
        )
    return out
