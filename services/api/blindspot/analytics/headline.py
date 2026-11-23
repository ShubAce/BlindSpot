"""The numbers on BlindSpot's front page, each computed from stored rows (GET /api/stats/headline).

One function per headline block. Each returns (block, sources) and reads the database only:

- fires_24h        FIRMS detections in the last 24 h inside India's Punjab, Haryana, Delhi and
                   Chandigarh polygons (never a lat/lon box: the box also holds Pakistan's Punjab),
                   persistent industrial heat sources excluded.
- season           the same clipped FIRMS count since 15 Sep beside CREAMS (ICAR-IARI) official
                   counts for the same IST days, so the UI shows the two side by side.
- unseen_burns     Sentinel-2 burn scars with no FIRMS detection: "candidate unseen burns", with
                   the published audit precision and its 95 % interval.
- delhi_air        Delhi's measured PM2.5 now (median of fresh stations) with its provider; the
                   last measured value with its age when nothing is fresh; never the model.
- passes           the last and next VIIRS/MODIS look at Punjab (skyfield/SGP4 from stored TLEs).
- gk2a_evening     GK-2A fire activity in the evening hours no polar satellite sees, in
                   pixel-hours (an activity index, not a fire count), with its validation.

A block whose inputs are missing says so (`available: false` plus `reason`); it never invents a
number. The router composes the blocks and catches a failing block so the rest still render.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics import fires_stats
from blindspot.analytics.burnscar import RULE, audit_summary
from blindspot.analytics.passes import (
    current_status,
    default_site,
    load_orbits,
    passes_for_site,
)
from blindspot.geo import IST
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch

Block = tuple[dict[str, Any], list[SourceRef]]

PASS_LOOKBACK_H = 24  # long enough that the last fire-sensor look is always inside the window
PASS_LOOKAHEAD_H = 36  # VIIRS/MODIS revisit Punjab several times a day; 36 h always holds the next


def _ist(t: datetime) -> str:
    """ISO-8601 in IST with its offset, for display ("2026-10-31T04:25+05:30")."""
    return t.astimezone(IST).isoformat(timespec="minutes")


# --- freshness ----------------------------------------------------------------------------------
# A rolling-window figure ("fires in the last 24 h") read from a table the jobs stopped filling
# would quietly shrink towards zero. So each block names the job that feeds it, when that job last
# succeeded, the time of the newest data behind the figure, and `stale` when the job has not
# succeeded for longer than its schedule allows (.github/workflows/data-jobs.yml: the hourly group
# runs at most 3 h apart at night, met every 6 h, Sentinel-2 and element sets daily) plus an hour
# for a late start. Additive fields: the figures themselves are unchanged.

FEEDS: dict[str, tuple[str, float]] = {  # block -> (job, hours after which it is stale)
    "fires_24h": ("firms_nrt", 4),
    "season": ("creams_daily", 4),
    "unseen_burns": ("sentinel", 26),
    "delhi": ("air", 4),
    "passes": ("tle", 26),
    "gk2a_evening": ("geostationary", 4),
}


def feed_status(session: Session) -> dict[str, dict[str, Any]]:
    """Per job in FEEDS: when it last ran and last succeeded (job_run), in one small query."""
    jobs = sorted({job for job, _ in FEEDS.values()})
    rows = session.execute(
        text(
            "SELECT name, max(finished_at) FILTER (WHERE ok) AS ok_at, max(finished_at) AS ran_at "
            "FROM job_run WHERE name = ANY(:jobs) GROUP BY name"
        ),
        {"jobs": jobs},
    ).mappings()
    return {r["name"]: {"last_success_at": r["ok_at"], "last_run_at": r["ran_at"]} for r in rows}


def freshness(block: str, status: dict[str, dict[str, Any]], now: datetime,
              newest_data_at: datetime | None) -> dict[str, Any]:
    """The `freshness` object of a headline block (see FEEDS)."""
    job, hours = FEEDS[block]
    seen = status.get(job) or {}
    ok_at = seen.get("last_success_at")
    return {
        "job": job,
        "last_success_at": ok_at,
        "last_run_at": seen.get("last_run_at"),
        "newest_data_at": newest_data_at,
        "stale_after_hours": hours,
        "stale": ok_at is None or now - ok_at > timedelta(hours=hours),
        "basis": "stale when the job has not succeeded within its schedule's longest gap plus an "
        "hour (job_run); newest_data_at is the newest observation behind the figure",
    }


# --- fires --------------------------------------------------------------------------------------


def fires_24h(session: Session, now: datetime) -> Block:
    """FIRMS detections in the 24 h before `now`, clipped to the AOI states' polygons."""
    t0 = now - timedelta(hours=24)
    rf = fires_stats.region_filter(session, "all")
    window = {"from_utc": t0, "to_utc": now, "from_ist": _ist(t0), "to_ist": _ist(now)}
    if not rf.available:
        return {"available": False, "reason": rf.note, "count": None, "window": window}, []
    where = f"f.acq_utc > :t0 AND f.acq_utc <= :t1 AND {rf.sql}"
    params = {"t0": t0, "t1": now, **rf.params}
    rows = session.execute(
        text(
            f"SELECT f.satellite, f.state, count(*) AS n, max(f.acq_utc) AS newest "
            f"FROM fire_detection f WHERE {where} GROUP BY 1, 2"
        ),
        params,
    ).all()
    by_sat: Counter[str] = Counter()
    by_state: Counter[str] = Counter()
    newest = None
    for satellite, state, n, latest in rows:
        by_sat[satellite] += n
        by_state[state] += n
        newest = latest if newest is None or latest > newest else newest
    static_where = "f.acq_utc > :t0 AND f.acq_utc <= :t1 AND lower(f.state) LIKE ANY(:region_states)"
    block = {
        "available": True,
        "count": sum(by_sat.values()),
        "by_satellite": {s: by_sat.get(s, 0) for s in fires_stats.FIRE_SATELLITES},
        "by_state": dict(by_state.most_common()),
        "window_ist": {"from": window["from_ist"], "to": window["to_ist"]},
        "window": {"from_utc": t0, "to_utc": now},
        "latest_acq_utc": newest,
        "static_excluded": fires_stats.static_excluded(session, static_where, params),
        "clip": "india",
        "region": "Punjab, Haryana, Delhi, Chandigarh (Survey of India polygons)",
        "note": "Every VIIRS/MODIS detection counts once per satellite: one fire seen by two "
        "satellites counts twice. Persistent industrial heat sources are excluded.",
    }
    return block, fires_stats.fire_refs(session, where, params)


def season(session: Session, now: datetime) -> Block:
    """Season-to-date FIRMS detections (clipped) beside CREAMS official events, same IST days."""
    today = now.astimezone(IST).date()
    first = fires_stats.season_start(today)
    firms_payload, firms_refs = fires_stats.daily_series(session, first, today, "all")
    official, official_refs = fires_stats.official_counts(session, first, today, "all")
    rf = fires_stats.region_filter(session, "all")
    firms_by_state: dict[str, int] = {}
    if rf.available:
        t0, t1 = fires_stats.ist_day_bounds(first, today)
        firms_by_state = {
            state: n
            for state, n in session.execute(
                text(
                    f"SELECT f.state, count(*) FROM fire_detection f "
                    f"WHERE f.acq_utc >= :t0 AND f.acq_utc < :t1 AND {rf.sql} "
                    "GROUP BY 1 ORDER BY 2 DESC"
                ),
                {"t0": t0, "t1": t1, **rf.params},
            ).all()
        }
    official_series = official.get("series", [])
    published = [s["date"] for s in official_series if s.get("published")]
    official_total = official.get("totals", {}).get("count")
    firms_total = firms_payload["totals"]["count"] if firms_payload.get("series") else None
    block = {
        "since": first.isoformat(),
        "until": today.isoformat(),
        "day_basis": "IST",
        "firms_detections": firms_total,
        "firms_by_state": firms_by_state,
        "firms_complete_days": firms_payload.get("complete_days"),
        "firms_days": len(firms_payload.get("series", [])),
        "static_excluded": firms_payload.get("static_excluded"),
        "official_events": official_total,
        "official_by_state": official.get("totals", {}).get("by_state", {}),
        "official_source": "CREAMS, ICAR-IARI (rice residue layer)",
        "official_last_layer": max(published) if published else None,
        "official_layers_missing": official.get("layer_totals", {}).get("layers_missing", []),
        "note": "FIRMS counts every satellite detection (several per fire); CREAMS publishes "
        "one event per fire after its own de-duplication. Both are clipped to the same Indian "
        "state polygons; days CREAMS has not published yet are listed in "
        "official_layers_missing.",
    }
    if firms_total is None:
        block["reason"] = firms_payload.get("note") or "no FIRMS detections stored"
    return block, [*firms_refs, *official_refs]


# --- burn scars -----------------------------------------------------------------------------------


def unseen_burns(session: Session) -> Block:
    """Season totals of Sentinel-2 burn scars, and the part no FIRMS detection explains."""
    exists = session.execute(text("SELECT to_regclass('public.burn_cluster') IS NOT NULL")).scalar()
    if not exists:
        return {"available": False, "reason": "burn scars not mapped yet (job sentinel)"}, []
    row = session.execute(
        text(
            """
            SELECT count(*) AS clusters, coalesce(sum(area_ha), 0) AS area_ha,
                   count(*) FILTER (WHERE unseen) AS unseen_clusters,
                   coalesce(sum(area_ha) FILTER (WHERE unseen), 0) AS unseen_area_ha,
                   coalesce(sum(area_ha) FILTER (WHERE unseen IS NOT NULL), 0) AS checked_area_ha,
                   min(burn_after) AS first_pass, max(burn_before) AS last_pass,
                   count(DISTINCT district) AS districts
            FROM burn_cluster WHERE model_version = :version
            """
        ),
        {"version": RULE.version},
    ).mappings().one()
    audit = audit_summary()
    unseen = audit["candidate_unseen"]
    checked = float(row["checked_area_ha"])
    block: dict[str, Any] = {
        "available": row["clusters"] > 0,
        "label": "candidate unseen burns",
        "clusters": row["clusters"],
        "area_ha": round(float(row["area_ha"]), 1),
        "unseen_clusters": row["unseen_clusters"],
        "unseen_area_ha": round(float(row["unseen_area_ha"]), 1),
        "checked_area_ha": round(checked, 1),
        "unseen_share": round(float(row["unseen_area_ha"]) / checked, 4) if checked else None,
        "districts": row["districts"],
        "first_pass": row["first_pass"],
        "last_pass": row["last_pass"],
        "model_version": RULE.version,
        "precision": unseen["precision"],
        "precision_ci95": unseen["precision_ci95"],
        "precision_n": unseen["audited"],
        "note": "Scars from Sentinel-2 passes processed so far (see /api/scars/summary coverage); "
        "unseen = no FIRMS detection near the scar between the passes that bracket it. Treat the "
        "share as an upper bound: audited precision is shown with its 95 % interval.",
    }
    if not row["clusters"]:
        block["reason"] = "no burn scars mapped yet (job sentinel)"
    return block, latest_refs(session, ["stac_e84", "firms"])


# --- air ------------------------------------------------------------------------------------------


def delhi_air(receptors: list[dict[str, Any]], city: str = "Delhi") -> dict[str, Any]:
    """Delhi's block from the /api/air/latest receptors: measured PM2.5 only, with its provider.

    When no station reported in the freshness window, `pm25` is null and `last` carries the most
    recent measured value and its age, so the UI can show it as stale. The CAMS model value and
    the CPCB AQI are different quantities and are never put in `pm25`.
    """
    entry = next((r for r in receptors if (r.get("city") or "").lower() == city.lower()), None)
    if entry is None:
        return {"city": city, "available": False, "pm25": None,
                "reason": f"no station data for {city} in air_obs yet (job air)"}  # fmt: skip
    aqi = entry.get("aqi") or None
    block = {
        "city": city,
        "available": entry.get("pm25_now") is not None or entry.get("pm25_last") is not None,
        "pm25": entry.get("pm25_now"),
        "unit": "µg/m³",
        "observed_at": entry.get("observed_at"),
        "provider": entry.get("provider"),
        "n_stations": entry.get("n_stations"),
        "stale": entry.get("pm25_now") is None,
        "last": entry.get("pm25_last"),
        "pm25_24h_mean": entry.get("pm25_24h_mean"),
        "aqi": {k: aqi.get(k) for k in ("value", "category", "observed_at", "n_stations")}
        if aqi
        else None,
        "basis": "median PM2.5 of the city's stations that reported within the freshness "
        "window (OpenAQ republishing CPCB/DPCC monitors)",
    }
    return block


def delhi_smoke(back_summaries: list[dict[str, Any]], city: str = "Delhi") -> dict[str, Any] | None:
    """The newest 72 h back-trajectory verdict for Delhi (two-model ensemble), if one exists."""
    row = next((r for r in back_summaries if (r.get("city") or "").lower() == city.lower()), None)
    if row is None:
        return None
    s = row.get("summary") or {}
    return {
        "run_at": row.get("run_at"),
        "verdict_hint": s.get("verdict_hint"),
        "verdict_reason": s.get("verdict_reason"),
        "share_over_burn_belt": s.get("share_over_burn_belt"),
        "fire_exposure_index": s.get("fire_exposure_index"),
        "fire_exposure_range": s.get("fire_exposure_range"),
    }


# --- satellites -----------------------------------------------------------------------------------


def passes(session: Session, now: datetime) -> Block:
    """Last and next VIIRS/MODIS look at the default site (Punjab's polygon centroid)."""
    orbits = load_orbits(session)
    if not orbits:
        return {"available": False, "reason": "no element sets yet (job tle)",
                "last": None, "next": None}, []  # fmt: skip
    site = default_site(session)
    found = passes_for_site(
        orbits, site.lat, site.lon,
        now - timedelta(hours=PASS_LOOKBACK_H), now + timedelta(hours=PASS_LOOKAHEAD_H),
    )  # fmt: skip
    status = current_status(found, now)
    refs = [ref_for_fetch(session, i) for i in sorted({o.fetch_id for o in orbits if o.fetch_id})]
    block = {
        "available": True,
        "site": {"lat": site.lat, "lon": site.lon, "name": site.name, "basis": site.basis},
        "last": status["last"],
        "next": status["next"],
        "watching": status["watching"],
        "blind_for_minutes": status["blind_for_minutes"],
        "instruments": ["VIIRS", "MODIS"],
        "note": "Fire-detecting sensors only (VIIRS, MODIS). Predicted from CelesTrak elements "
        "with SGP4; checked against FIRMS acquisition times (/api/orbits/validation).",
    }
    return block, [r for r in refs if r is not None] or latest_refs(session, ["celestrak"])


# --- geostationary ----------------------------------------------------------------------------------


def gk2a_evening(diurnal: dict[str, Any]) -> dict[str, Any]:
    """The evening-activity block from a /api/geostationary/diurnal payload.

    Reported in GK-2A pixel-hours (fire pixels x hours), an activity index: GK-2A is too coarse to
    count fires, so the block carries its validation against VIIRS and MODIS Aqua beside it.
    """
    if not diurnal.get("scans"):
        return {"available": False,
                "reason": diurnal.get("reason") or "no GK-2A scans analysed yet"}  # fmt: skip
    gaps = diurnal.get("unobserved_gaps_ist") or []
    evening = max(gaps, key=lambda g: g.get("pixel_hours") or 0, default=None)
    validation = diurnal.get("validation") or {}
    aqua = (validation.get("groups") or {}).get("modis_aqua_day")
    return {
        "available": True,
        "unit": diurnal.get("unit"),
        "days": diurnal.get("days"),
        "since": diurnal.get("since"),
        "scans": diurnal.get("scans"),
        "peak_ist": diurnal.get("peak_ist"),
        "pixel_hours": diurnal.get("pixel_hours"),
        "pixel_hours_outside_polar_windows": diurnal.get("pixel_hours_outside_polar_windows"),
        "share_outside_polar_windows": diurnal.get("share_outside_polar_windows"),
        "unobserved_span_ist": evening,
        "validation": {
            "viirs_hit_rate": validation.get("hit_rate"),
            "viirs_false_alarm_rate": validation.get("false_alarm_rate"),
            "viirs_n": validation.get("n"),
            "modis_aqua_day": aqua,
        },
        "note": "An activity index in GK-2A pixel-hours, not a fire count: GK-2A misses most "
        "small midday fires that VIIRS sees and is reliable for strong afternoon fires.",
    }
