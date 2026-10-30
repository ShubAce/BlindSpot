"""GET /api/risk: harvested fields whose straw is still lying there, ranked by burn risk, and the
villages with the most hectares at risk.

The scores are computed by the `risk` job (blindspot/jobs/risk.py, explained in
blindspot/analytics/risk.py) and stored on harvest_cluster; this route only reads them. A field's
village is the one the sentinel module stored, or else the nearest village or town within ~5 km
(marked `village_inferred`), never a guess beyond that.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Query
from sqlalchemy import text

from blindspot.analytics import risk as model
from blindspot.api import BBoxDep, SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.api.responses import ORJSONResponse
from blindspot.config import get_settings
from blindspot.provenance import latest_refs, ref_for_fetch, utcnow

router = APIRouter(prefix="/api/risk", tags=["risk"])

RANKING_LIMIT = 50
SOURCE_KEYS = ["stac_e84", "firms", "openmeteo"]

METHOD = {
    "formula": "risk = 100 x dryness x (0.4 timing + 0.4 neighbourhood + 0.2 history)",
    "model": model.MODEL_VERSION,
    "weights": {"timing": model.PRIOR.timing, "neighbourhood": model.PRIOR.neighbourhood,
                "history": model.PRIOR.history},
    "factors": {
        "timing": f"1.0 up to {model.WINDOW_FULL_DAYS:g} days after harvest, falling to 0 at "
                  f"{model.WINDOW_CLOSED_DAYS:g} days (harvest-to-wheat-sowing gap of 10-30 days)",
        "neighbourhood": f"burning within {model.NEIGHBOUR_RADIUS_KM:g} km in the last {model.NEIGHBOUR_DAYS} "
                         "days, seen two ways and combined as either-one (noisy-OR): FIRMS fires "
                         "(nominal/high confidence) plus candidate unseen burns, and hectares of "
                         "Sentinel-2 burn scars; each as 1 - exp(-x / today's typical x)",
        "history": f"FIRMS vegetation fires within {model.NEIGHBOUR_RADIUS_KM:g} km last season "
                   "(15 Sep - 30 Nov); 1 - exp(-n / typical n)",
        "dryness": f"exp(-rain in the next 72 h / {model.HEAVY_RAIN_MM:g} mm); multiplies the rest",
    },
    "note": "A transparent ranking, not a probability: it orders today's fields by how likely they "
            "are to burn next. See `backtest` for how well that ordering worked on this season.",
}

_FIELDS_SQL = """
    WITH h AS (
        SELECT h.*,
               coalesce(h.village, np.name) AS village_name,
               coalesce(h.district, np.district) AS district_name,
               (h.village IS NULL AND np.name IS NOT NULL) AS village_inferred
        FROM harvest_cluster h
        LEFT JOIN LATERAL (
            SELECT p.name, p.district FROM place p
            WHERE h.village IS NULL AND p.kind IN ('village', 'town')
              AND p.geom && ST_Expand(h.centroid, 0.05)
            ORDER BY p.geom <-> h.centroid LIMIT 1
        ) np ON true
        WHERE h.centroid && ST_MakeEnvelope(:w, :s, :e, :n, 4326)
          AND (CAST(:district AS text) IS NULL OR coalesce(h.district, np.district) ILIKE :district)
          AND coalesce(h.risk_score, 0) >= :min_score
    )
"""


@router.get("")
@cached_route(ttl=60)
def risk(
    session: SessionDep,
    bbox: BBoxDep,
    district: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    min_score: Annotated[float, Query(ge=0, le=100)] = 0.0,
) -> ORJSONResponse:
    """Harvested-but-unburnt fields ranked by burn risk (highest first), with the per-village ranking."""
    sources = latest_refs(session, SOURCE_KEYS)
    exists = session.execute(text("SELECT to_regclass('public.harvest_cluster') IS NOT NULL")).scalar_one()
    if not exists:
        return respond(_empty("harvest_cluster does not exist yet (the sentinel module creates it)"), sources)
    params: dict[str, Any] = {"w": bbox[0], "s": bbox[1], "e": bbox[2], "n": bbox[3],
                              "district": district.strip() if district else None, "min_score": min_score}
    totals = session.execute(text(_FIELDS_SQL + """
        SELECT count(*), coalesce(sum(area_ha), 0), count(risk_score),
               coalesce(sum(area_ha) FILTER (WHERE risk_score IS NOT NULL), 0),
               max(risk_factors ->> 'as_of')
        FROM h
    """), params).one()
    if totals[0] == 0:
        any_rows = session.execute(text("SELECT EXISTS (SELECT 1 FROM harvest_cluster)")).scalar_one()
        reason = "no harvested-but-unburnt fields match these filters (bbox/district/min_score)" if any_rows \
            else "no harvested-but-unburnt fields have been mapped yet"
        return respond(_empty(reason), sources, max_age=60)
    rows = session.execute(text(_FIELDS_SQL + """
        SELECT id, ST_AsGeoJSON(geom, 6)::json, area_ha, harvested_after, harvested_before, last_seen_unburnt,
               risk_score, risk_factors, district_name, village_name, village_inferred,
               ST_X(centroid), ST_Y(centroid), mgrs_tile, state
        FROM h ORDER BY risk_score DESC NULLS LAST, area_ha DESC, id LIMIT :limit
    """), {**params, "limit": limit}).all()
    now = utcnow()
    features = [_feature(r, now) for r in rows]
    sources = _cite_scoring_fetches(session, rows, sources)
    ranking = session.execute(text(_FIELDS_SQL + """
        SELECT village_name, district_name, count(*), sum(area_ha),
               sum(risk_score * area_ha) / nullif(sum(area_ha), 0), max(risk_score),
               sum(risk_score / 100.0 * area_ha),
               ST_X(ST_Centroid(ST_Collect(centroid))), ST_Y(ST_Centroid(ST_Collect(centroid))),
               bool_or(village_inferred)
        FROM h WHERE risk_score IS NOT NULL AND village_name IS NOT NULL
        GROUP BY village_name, district_name
        ORDER BY 7 DESC NULLS LAST, 5 DESC LIMIT :rlimit
    """), {**params, "rlimit": RANKING_LIMIT}).all()
    unnamed = session.execute(text(_FIELDS_SQL + """
        SELECT count(*), coalesce(sum(area_ha), 0) FROM h WHERE village_name IS NULL
    """), params).one()
    payload = {
        "as_of": _parse_time(totals[4]),
        "totals": {"clusters": totals[0], "area_ha": round(float(totals[1]), 2), "scored": totals[2],
                   "scored_area_ha": round(float(totals[3]), 2)},
        "features": {"type": "FeatureCollection", "features": features},
        "ranking": [
            {"village": r[0], "district": r[1], "clusters": r[2], "area_ha": round(float(r[3]), 2),
             "risk_score": _round(r[4], 1), "max_risk_score": _round(r[5], 1),
             "risk_weighted_ha": _round(r[6], 2), "centroid": [_round(r[7], 6), _round(r[8], 6)],
             "village_inferred": bool(r[9])}
            for r in ranking
        ],
        "unnamed": {"clusters": unnamed[0], "area_ha": round(float(unnamed[1]), 2),
                    "why": "no village or town within ~5 km in the place table (or places not loaded yet)"},
        "ranking_order": "risk_weighted_ha = sum over a village's fields of risk_score/100 x area_ha",
        "method": METHOD,
        "backtest": _backtest_summary(),
        "reason": None if totals[2] else "fields are mapped but not scored yet (the risk job has not run)",
    }
    return respond(payload, sources, max_age=60)


def _feature(r: Any, now: datetime) -> dict[str, Any]:
    factors = r[7] or None
    days = None
    if factors:
        days = (factors.get("timing") or {}).get("days_since_harvest")
    elif r[3] is not None and r[4] is not None:  # not scored yet: say how long ago, from the dates
        days = round(model.days_since_harvest(now, r[3], r[4])[0], 1)
    return {
        "type": "Feature",
        "id": r[0],
        "geometry": r[1],
        "properties": {
            "id": r[0], "area_ha": round(float(r[2]), 3), "harvested_after": r[3], "harvested_before": r[4],
            "last_seen_unburnt": r[5], "days_since_harvest": days, "risk_score": _round(r[6], 1),
            "risk_factors": factors, "district": r[8], "village": r[9], "village_inferred": bool(r[10]),
            "centroid": [_round(r[11], 6), _round(r[12], 6)], "mgrs_tile": r[13], "state": r[14],
        },
    }


def _cite_scoring_fetches(session: Any, rows: list[Any], sources: list[Any]) -> list[Any]:
    """Add the exact upstream calls the scores used (the rain forecast and the FIRMS archive
    pages, recorded by the risk job in risk_factors.fetch_ids) to the response's sources."""
    factors = next((r[7] for r in rows if r[7]), None) or {}
    ids = [i for group in (factors.get("fetch_ids") or {}).values() for i in (group or [])]
    seen = {s.fetch_id for s in sources}
    for fid in dict.fromkeys(ids):
        if fid in seen:
            continue
        ref = ref_for_fetch(session, fid)
        if ref is not None:
            sources.append(ref)
    return sources


def _empty(reason: str) -> dict[str, Any]:
    return {"as_of": None, "totals": {"clusters": 0, "area_ha": 0.0, "scored": 0, "scored_area_ha": 0.0},
            "features": {"type": "FeatureCollection", "features": []}, "ranking": [],
            "method": METHOD, "backtest": _backtest_summary(), "reason": reason}


def _backtest_summary() -> dict[str, Any] | None:
    """The headline of the last backtest the `risk` job wrote (a small JSON file), or None."""
    try:
        report = orjson.loads(get_settings().cache_path(*model.BACKTEST_FILE).read_bytes())
    except (OSError, ValueError):
        return None
    return {"computed_at": report.get("computed_at"), "headline": report.get("headline"),
            "all_pairs": report.get("all_pairs"), "timing_tested": report.get("timing_tested"),
            "timing_note": report.get("timing_note"), "reason": report.get("reason"),
            "windows": len(report.get("windows", [])), "latest_attempt": report.get("latest_attempt"),
            "method": report.get("method")}


def _round(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
