"""Statistics over stored fire detections, shared by /api/fires/* and the agent's tools.

- The season curve: detections per IST day with fire radiative power, flagged complete or not.
- The hour-of-day profile, the time windows in which satellites actually reported fires, and the
  longest stretch of the IST clock with (almost) no reports: the blind span.
- Official CREAMS counts per day, by satellite, state and district.
- Persistent heat sources (kilns, factories, flares) found from repeated night detections, which
  are left out of crop-fire counts by default and always reported as `static_excluded`.

Every count is clipped to India's state polygons through the state labels our boundary polygons
gave each row (SYNTHESIS rule: the AOI box also holds Pakistan's Punjab, which inflated counts
2.4x). Before boundaries are loaded, counts are withheld with a reason rather than taken over a
box. Days are IST days, because a day of burning in Punjab is an IST day.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.geo import IST
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch

# Official crop-residue fire monitoring window: 15 Sep - 30 Nov, "standard protocol developed by
# ISRO" (PIB press release, 1 Dec 2025, https://www.pib.gov.in/PressReleasePage.aspx?PRID=2197201).
SEASON_START = (9, 15)  # month, day

FIRMS_SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT")
FIRE_SATELLITES = ("Suomi NPP", "NOAA-20", "NOAA-21", "Aqua", "Terra")

# Region -> lower-case LIKE patterns on the state label. "all" is the AOI's states: Punjab,
# Haryana, Delhi and Chandigarh (the shapeISO set IN-PB, IN-HR, IN-DL, IN-CH in SYNTHESIS).
REGION_STATES: dict[str, tuple[str, ...]] = {
    "punjab": ("punjab",),
    "haryana": ("haryana",),
    "delhi": ("%delhi%",),
    "chandigarh": ("chandigarh",),
}
REGION_STATES["all"] = tuple(p for v in REGION_STATES.values() for p in v)
REGIONS = ("all", "punjab", "haryana", "delhi", "chandigarh")

NO_BOUNDARIES = (
    "State boundaries are not loaded yet, so detections cannot be clipped to Indian states. "
    "Counts are withheld instead of being taken over a box that also covers Pakistan's Punjab."
)

# Observed-window rules (hours endpoint), stated in every response:
WINDOW_GAP_MIN = 30  # report times further apart than this start a new window
MIN_WINDOW_SHARE = 0.01  # windows holding <1% of detections do not end the blind span


def season_start(today: date) -> date:
    """The 15 September that opened the current (or most recent) monitoring season."""
    year = today.year if (today.month, today.day) >= SEASON_START else today.year - 1
    return date(year, *SEASON_START)


def ist_day_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    """UTC instants bounding IST days first..last inclusive (00:00 IST of `first` to 00:00 IST
    of the day after `last`)."""
    start = datetime.combine(first, time(0), IST).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0), IST).astimezone(UTC)
    return start, end


def boundaries_loaded(session: Session) -> bool:
    return bool(
        session.execute(
            text("SELECT EXISTS (SELECT 1 FROM boundary WHERE level IN ('state', 'district'))")
        ).scalar()
    )


@dataclass(frozen=True)
class RegionFilter:
    """SQL conditions on table alias `f` that keep one region's crop-fire detections."""

    region: str
    available: bool  # False before boundaries load: callers return no counts, with `note`
    sql: str  # the region, minus persistent heat sources unless include_static
    params: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    region_sql: str = "false"  # the region alone, to count what the static exclusion removed
    include_static: bool = False


def region_filter(session: Session, region: str, include_static: bool = False) -> RegionFilter:
    if region not in REGION_STATES:
        raise ValueError(f"region must be one of {REGIONS}")
    if not boundaries_loaded(session):
        return RegionFilter(region, False, "false", {}, NO_BOUNDARIES, "false", include_static)
    region_sql = "lower(f.state) LIKE ANY(:region_states)"
    sql = region_sql if include_static else f"{region_sql} AND NOT {STATIC_SQL}"
    return RegionFilter(
        region, True, sql, {"region_states": list(REGION_STATES[region])}, None, region_sql,
        include_static,
    )  # fmt: skip


def _static_summary(session: Session, rf: RegionFilter, window_sql: str,
                    params: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
    """How many detections the static-source rule removed from these counts, and the rule."""
    removed = 0
    if rf.available and not rf.include_static:
        removed = static_excluded(session, f"{window_sql} AND {rf.region_sql}", {**params, **rf.params})
    return {"static_excluded": removed, "static_rule": STATIC_RULE,
            "include_static": rf.include_static}  # fmt: skip


def fire_refs(session: Session, where: str, params: Mapping[str, Any]) -> list[SourceRef]:
    """Provenance for a set of fire_detection rows: the newest fetch behind each FIRMS product in
    the set, plus FIRMS's latest successful check (which says how fresh the data is)."""
    ids = session.execute(
        text(
            f"SELECT DISTINCT ON (f.source) f.fetch_id FROM fire_detection f "
            f"WHERE {where} AND f.fetch_id IS NOT NULL ORDER BY f.source, f.fetch_id DESC"
        ),
        dict(params),
    ).scalars()
    refs = latest_refs(session, ["firms"])
    seen = {r.fetch_id for r in refs}
    for fetch_id in ids:
        if fetch_id not in seen:
            ref = ref_for_fetch(session, fetch_id)
            if ref is not None:
                refs.append(ref)
                seen.add(fetch_id)
    return refs


# --- season curve --------------------------------------------------------------------------------


def complete_ist_days(session: Session, first: date, last: date) -> set[date]:
    """IST days whose detections are all in: every FIRMS product's UTC days overlapping that IST
    day (the day before and the day itself) were fetched complete through the area API."""
    rows = session.execute(
        text(
            "SELECT source, day FROM fire_coverage "
            "WHERE complete AND day BETWEEN :d0 AND :d1 AND source = ANY(:sources)"
        ),
        {"d0": first - timedelta(days=1), "d1": last, "sources": list(FIRMS_SOURCES)},
    ).all()
    have: dict[date, set[str]] = defaultdict(set)
    for source, day in rows:
        have[day].add(source)
    full = {d for d, s in have.items() if len(s) == len(FIRMS_SOURCES)}
    out, d = set(), first
    while d <= last:
        if d in full and d - timedelta(days=1) in full:
            out.add(d)
        d += timedelta(days=1)
    return out


def daily_series(
    session: Session, first: date, last: date, region: str, include_static: bool = False
) -> tuple[dict[str, Any], list[SourceRef]]:
    """Detections and FRP per IST day, first..last, with per-satellite counts and completeness.
    Returns the payload and the fetches behind it."""
    rf = region_filter(session, region, include_static)
    t0, t1 = ist_day_bounds(first, last)
    complete = complete_ist_days(session, first, last)
    per_day: dict[date, Counter[str]] = defaultdict(Counter)
    frp: dict[date, float] = defaultdict(float)
    if rf.available:
        rows = session.execute(
            text(
                f"""
                SELECT (f.acq_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, f.satellite,
                       count(*) AS n, coalesce(sum(f.frp_mw), 0) AS frp
                FROM fire_detection f
                WHERE f.acq_utc >= :t0 AND f.acq_utc < :t1 AND {rf.sql}
                GROUP BY 1, 2
                """
            ),
            {"t0": t0, "t1": t1, **rf.params},
        ).all()
        for day, satellite, n, frp_sum in rows:
            per_day[day][satellite] += n
            frp[day] += float(frp_sum)
    series = []
    d = first
    while d <= last and rf.available:
        counts = per_day.get(d, Counter())
        series.append(
            {
                "date": d.isoformat(),
                "count": sum(counts.values()),
                "frp_sum": round(frp.get(d, 0.0), 2),
                "by_satellite": {s: counts.get(s, 0) for s in FIRE_SATELLITES},
                "complete": d in complete,
            }
        )
        d += timedelta(days=1)
    payload = {
        "region": region,
        "since": first.isoformat(),
        "until": last.isoformat(),
        "day_basis": "IST",
        "series": series,
        "totals": {
            "count": sum(s["count"] for s in series),
            "frp_sum": round(sum(s["frp_sum"] for s in series), 2),
        },
        "complete_days": sum(1 for s in series if s["complete"]),
        **_static_summary(session, rf, "f.acq_utc >= :t0 AND f.acq_utc < :t1", {"t0": t0, "t1": t1}),
        "note": rf.note,
    }
    where = f"f.acq_utc >= :t0 AND f.acq_utc < :t1 AND {rf.sql}"
    return payload, fire_refs(session, where, {"t0": t0, "t1": t1, **rf.params})


# --- hour of day, observed windows and the blind span --------------------------------------------


def _hhmm(minute: int) -> str:
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def observed_windows(
    minute_counts: Mapping[int, int],
    minute_satellites: Mapping[int, Iterable[str]] | None = None,
    minute_daynight: Mapping[int, Mapping[str, int]] | None = None,
    *,
    gap_min: int = WINDOW_GAP_MIN,
    min_share: float = MIN_WINDOW_SHARE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Windows of the IST clock in which fires were reported, the gaps between them, and the
    blind span. Pure function over {minute of day (IST): detections}.

    - Minutes closer than `gap_min` join one window (the clock wraps at midnight). Each window
      carries its share of all detections, its satellites and whether its detections are mostly
      daytime ("D") or night ("N") by FIRMS's own flag.
    - Windows holding less than `min_share` are `minor`: a stray report (Terra's 20:56 IST night
      pass saw one Punjab fire in a week) is listed inside a gap instead of splitting it.
    - The blind span is the gap that starts when the day's last daytime window ends. Crop
      residue is lit in daylight (99% of detections here carry FIRMS's day flag), so a field lit
      after that last daytime look goes unreported until the next window, if it still burns.
      With no daytime window it falls back to the longest gap.
    """
    minutes = sorted(m % 1440 for m, n in minute_counts.items() if n > 0)
    total = sum(n for n in minute_counts.values() if n > 0)
    if not minutes:
        return [], [], None
    clusters: list[list[int]] = [[minutes[0]]]
    for m in minutes[1:]:
        if m - clusters[-1][-1] > gap_min:
            clusters.append([m])
        else:
            clusters[-1].append(m)
    if len(clusters) > 1 and minutes[0] + 1440 - minutes[-1] <= gap_min:
        clusters[0] = clusters.pop() + clusters[0]  # one window across midnight
    sats = minute_satellites or {}
    flags = minute_daynight or {}
    windows = []
    for cluster in clusters:
        count = sum(minute_counts.get(m, 0) for m in cluster)
        dn: Counter[str] = Counter()
        for m in cluster:
            dn.update(flags.get(m, {}))
        windows.append(
            {
                "start": _hhmm(cluster[0]),
                "end": _hhmm(cluster[-1]),
                "count": count,
                "share": round(count / total, 4),
                "satellites": sorted({s for m in cluster for s in sats.get(m, ())}),
                "daynight": dn.most_common(1)[0][0] if dn else None,
                "minor": count / total < min_share,
                "_start": cluster[0],
                "_end": cluster[-1],
            }
        )
    windows.sort(key=lambda w: w["_start"])
    major = [w for w in windows if not w["minor"]] or windows

    gaps = []
    for i, w in enumerate(major):
        nxt = major[(i + 1) % len(major)]
        length = (nxt["_start"] - w["_end"]) % 1440 or (1440 if len(major) == 1 else 0)
        if length <= 0:
            continue
        inside = [
            m for m in windows
            if m["minor"] and 0 < (m["_start"] - w["_end"]) % 1440 < length
        ]  # fmt: skip
        n_inside = sum(m["count"] for m in inside)
        gaps.append(
            {
                "start": w["end"],
                "end": nxt["start"],
                "hours": round(length / 60, 2),
                "detections": n_inside,
                "share": round(n_inside / total, 4),
                "minor_windows": [{"start": m["start"], "end": m["end"], "count": m["count"],
                                   "satellites": m["satellites"]} for m in inside],  # fmt: skip
                "after_daytime": w["daynight"] == "D" and nxt["daynight"] != "D",
                "_from": w["_end"],
            }
        )
    span = None
    rule = ""
    daytime = [w for w in major if w["daynight"] == "D"]
    if daytime and gaps:
        last_day_end = max(w["_end"] for w in daytime)  # daytime windows do not cross midnight
        span = next((g for g in gaps if g["_from"] == last_day_end), None)
        rule = "from the end of the last daytime window to the next window"
    if span is None and gaps:
        span = max(gaps, key=lambda g: g["hours"])
        rule = "longest gap between windows (no daytime window in the data)"
    for item in windows + gaps:
        for k in ("_start", "_end", "_from"):
            item.pop(k, None)
    if span is not None:
        span = {**span, "rule": rule}
    return windows, gaps, span


def hours_profile(
    session: Session, days: int, region: str, end: datetime, include_static: bool = False
) -> tuple[dict[str, Any], list[SourceRef]]:
    """Detections by IST hour over the `days` days before `end`, per satellite, with the
    observed windows and the blind span derived from the detections themselves."""
    rf = region_filter(session, region, include_static)
    t0 = end - timedelta(days=days)
    minute_counts: Counter[int] = Counter()
    minute_sats: dict[int, set[str]] = defaultdict(set)
    minute_dn: dict[int, Counter[str]] = defaultdict(Counter)
    by_satellite: dict[str, list[int]] = {s: [0] * 24 for s in FIRE_SATELLITES}
    if rf.available:
        rows = session.execute(
            text(
                f"""
                SELECT f.satellite,
                       (extract(hour FROM f.acq_utc AT TIME ZONE 'Asia/Kolkata') * 60
                        + extract(minute FROM f.acq_utc AT TIME ZONE 'Asia/Kolkata'))::int AS m,
                       f.daynight, count(*) AS n
                FROM fire_detection f
                WHERE f.acq_utc >= :t0 AND f.acq_utc < :t1 AND {rf.sql}
                GROUP BY 1, 2, 3
                """
            ),
            {"t0": t0, "t1": end, **rf.params},
        ).all()
        for satellite, minute, daynight, n in rows:
            minute_counts[minute] += n
            minute_sats[minute].add(satellite)
            minute_dn[minute][daynight] += n
            by_satellite.setdefault(satellite, [0] * 24)[minute // 60] += n
    bins = [0] * 24
    for minute, n in minute_counts.items():
        bins[minute // 60] += n
    windows, gaps, span = observed_windows(minute_counts, minute_sats, minute_dn)
    night = sum(n for dn in minute_dn.values() for flag, n in dn.items() if flag == "N")
    payload = {
        "days": days,
        "region": region,
        "window": {"from_utc": t0, "to_utc": end},
        "total": sum(bins),
        "bins": [{"hour_ist": h, "count": n} for h, n in enumerate(bins)],
        "by_satellite": by_satellite,
        "daytime_share": round(1 - night / sum(bins), 4) if sum(bins) else None,
        "observed_windows_ist": windows,
        "unobserved_span_ist": span,
        "gaps_ist": gaps,
        **_static_summary(session, rf, "f.acq_utc >= :t0 AND f.acq_utc < :t1", {"t0": t0, "t1": end}),
        "method": {
            "window_gap_minutes": WINDOW_GAP_MIN,
            "min_window_share": MIN_WINDOW_SHARE,
            "basis": "FIRMS acquisition times (IST clock) and day/night flags of the region's "
            "detections; the blind span starts when the last daytime window ends",
        },
        "note": rf.note,
    }
    where = f"f.acq_utc >= :t0 AND f.acq_utc < :t1 AND {rf.sql}"
    return payload, fire_refs(session, where, {"t0": t0, "t1": end, **rf.params})


# --- persistent (non-crop) heat sources ----------------------------------------------------------

# Research doc section 6.4: "a 400 m cell with detections on >= 3 nights ... is masked. This mirrors
# FIRMS's own static-thermal-anomaly approach." Night detections only, on purpose: crop residue is
# burnt in daylight, while neighbouring fields can easily burn on several different days.
STATIC_RADIUS_KM = 0.4
STATIC_MIN_NIGHTS = 3
STATIC_RULE = (
    f"Detections within {STATIC_RADIUS_KM * 1000:.0f} m of a spot where FIRMS saw heat on "
    f"{STATIC_MIN_NIGHTS} or more different nights are treated as industrial or urban heat "
    "sources (kilns, factories, flares), not crop fires, and left out of counts unless "
    "include_static=true."
)
STATIC_SQL = "EXISTS (SELECT 1 FROM fire_static_site s WHERE ST_Intersects(s.area, f.geom))"
EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius


@dataclass(frozen=True)
class StaticSite:
    members: list[tuple[float, float]]  # (lat, lon) of the night detections that recur
    lat: float
    lon: float
    nights: int  # distinct nights with a night detection within the radius
    night_detections: int
    first_night: date
    last_night: date


def night_of(acq_utc: datetime) -> date:
    """The evening a night begins on (IST): Terra at 20:56 and VIIRS at 01:30 the same night share
    one date, so one long fire is not counted as two nights."""
    return (acq_utc + timedelta(hours=5, minutes=30) - timedelta(hours=12)).date()


def find_static_sites(points: Sequence[tuple[float, float, date]]) -> list[StaticSite]:
    """Night detections (lat, lon, night) -> persistent heat sources. Pure function.

    1. For every night detection, count the distinct nights among night detections within
       400 m (a k-d tree on a local flat projection, accurate to a few metres at this scale).
    2. Detections whose neighbourhood spans 3+ nights recur; recurring detections within 400 m of
       each other form one site (connected components).
    """
    if not points:
        return []
    # scipy is imported here, not at module level: only the firms job calls this, and the web API
    # (which imports this module for its SQL helpers) must start without scipy on serverless.
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    lat = np.array([p[0] for p in points], dtype=float)
    lon = np.array([p[1] for p in points], dtype=float)
    nights = [p[2] for p in points]
    k = np.cos(np.radians(lat.mean()))
    xy = np.column_stack([EARTH_RADIUS_KM * k * np.radians(lon), EARTH_RADIUS_KM * np.radians(lat)])
    neighbours = cKDTree(xy).query_ball_point(xy, r=STATIC_RADIUS_KM)
    recurring = np.array(
        [i for i, nb in enumerate(neighbours) if len({nights[j] for j in nb}) >= STATIC_MIN_NIGHTS],
        dtype=int,
    )
    if recurring.size == 0:
        return []
    pairs = cKDTree(xy[recurring]).query_pairs(r=STATIC_RADIUS_KM, output_type="ndarray")
    graph = coo_matrix(
        (np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])) if len(pairs) else ([], ([], [])),
        shape=(recurring.size, recurring.size),
    )
    _, labels = connected_components(graph, directed=False)
    sites = []
    for label in np.unique(labels):
        members = recurring[labels == label]
        around = sorted(set().union(*(neighbours[i] for i in members)))
        site_nights = sorted({nights[j] for j in around})
        sites.append(
            StaticSite(
                members=[(float(lat[i]), float(lon[i])) for i in members],
                lat=round(float(lat[members].mean()), 5),
                lon=round(float(lon[members].mean()), 5),
                nights=len(site_nights),
                night_detections=len(around),
                first_night=site_nights[0],
                last_night=site_nights[-1],
            )
        )
    return sorted(sites, key=lambda s: -s.nights)


def static_excluded(session: Session, where: str, params: Mapping[str, Any]) -> int:
    """How many detections matching `where` lie in a persistent heat-source zone."""
    return int(
        session.execute(
            text(f"SELECT count(*) FROM fire_detection f WHERE {where} AND {STATIC_SQL}"),
            dict(params),
        ).scalar()
        or 0
    )


# --- sensor heartbeat ----------------------------------------------------------------------------


def sensor_heartbeat(session: Session) -> dict[str, datetime]:
    """Newest acquisition per satellite anywhere in FIRMS's South Asia files, as recorded by the
    firms_nrt job. Says whether an instrument is still producing data (SYNTHESIS: drive the
    clock's MODIS rows from actual FIRMS presence, not from orbits alone)."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (meta->>'product') meta->'newest_by_satellite' AS newest
            FROM source_fetch
            WHERE source_key = 'firms' AND ok AND meta ? 'newest_by_satellite'
            ORDER BY meta->>'product', started_at DESC
            """
        )
    ).scalars()
    out: dict[str, datetime] = {}
    for newest in rows:
        for satellite, stamp in (newest or {}).items():
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if satellite not in out or when > out[satellite]:
                out[satellite] = when
    return out


# --- official CREAMS counts ----------------------------------------------------------------------


def official_counts(
    session: Session, first: date, last: date, region: str
) -> tuple[dict[str, Any], list[SourceRef]]:
    """CREAMS official events per IST day (layer date), by satellite, state and district."""
    rf = region_filter(session, region)
    layers = {
        r.layer_date: r
        for r in session.execute(
            text(
                "SELECT layer_date, events, published, final, fetched_at, fetch_id "
                "FROM official_layer WHERE layer = 'rice' AND layer_date BETWEEN :d0 AND :d1"
            ),
            {"d0": first, "d1": last},
        )
    }
    per_day: dict[date, Counter[str]] = defaultdict(Counter)
    by_state: Counter[str] = Counter()
    by_district: Counter[tuple[str, str]] = Counter()
    if rf.available:
        rows = session.execute(
            text(
                f"""
                SELECT f.layer_date, f.satellite, f.state,
                       coalesce(f.district, initcap(f.creams_district)) AS district, count(*) AS n
                FROM official_fire_event f
                WHERE f.layer = 'rice' AND f.layer_date BETWEEN :d0 AND :d1 AND {rf.sql}
                GROUP BY 1, 2, 3, 4
                """
            ),
            {"d0": first, "d1": last, **rf.params},
        ).all()
        for day, satellite, state, district, n in rows:
            per_day[day][satellite] += n
            by_state[state] += n
            by_district[(district or "unknown", state)] += n
    layer_total = sum(r.events for r in layers.values() if r.published)
    series = []
    d = first
    while d <= last:
        layer = layers.get(d)
        counts = per_day.get(d, Counter())
        series.append(
            {
                "date": d.isoformat(),
                "count": sum(counts.values()) if rf.available else None,
                "by_satellite": dict(counts),
                "layer_events": layer.events if layer is not None and layer.published else None,
                "published": None if layer is None else layer.published,
                "final": None if layer is None else layer.final,
            }
        )
        d += timedelta(days=1)
    sources = latest_refs(session, ["creams"])
    seen = {r.fetch_id for r in sources}
    for fetch_id in sorted({r.fetch_id for r in layers.values() if r.fetch_id}, reverse=True)[:3]:
        ref = ref_for_fetch(session, fetch_id)
        if ref is not None and fetch_id not in seen:
            sources.append(ref)
    payload = {
        "layer": "rice",
        "region": region,
        "since": first.isoformat(),
        "until": last.isoformat(),
        "day_basis": "IST",
        "series": series,
        "totals": {
            "count": sum(by_state.values()) if rf.available else None,
            "by_state": dict(by_state.most_common()),
            "by_satellite": dict(sum(per_day.values(), Counter()).most_common()),
        },
        "by_district": [
            {"district": dist, "state": state, "count": n}
            for (dist, state), n in by_district.most_common(25)
        ],
        "layer_totals": {
            "events": layer_total,
            "layers_fetched": sum(1 for r in layers.values() if r.published),
            "layers_missing": sorted(d.isoformat() for d, r in layers.items() if not r.published),
            "note": "All events in the CREAMS layers, every state CREAMS monitors.",
        },
        "note": rf.note,
    }
    return payload, sources
