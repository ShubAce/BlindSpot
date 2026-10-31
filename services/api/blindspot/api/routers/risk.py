"""GET /api/risk: harvested fields whose straw is still lying there, ranked by burn risk, and the
villages with the most hectares at risk.

The scores are computed by the `risk` job (blindspot/jobs/risk.py, explained in
blindspot/analytics/risk.py) and stored on harvest_cluster (each field's own numbers) and risk_run
(what a run shares); this route only reads them and puts each field's factors back together. A field's
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
from blindspot.models.risk import latest_backtest_summary, latest_run
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
                         "days, seen two ways and averaged: FIRMS fires (nominal/high confidence) plus "
                         "candidate unseen burns, and hectares of Sentinel-2 burn scars; each as "
                         "x / (x + today's typical x)",
        "history": f"FIRMS vegetation fires within {model.NEIGHBOUR_RADIUS_KM:g} km last season "
                   "(15 Sep - 30 Nov); n / (n + typical n)",
        "dryness": f"exp(-rain in the next 72 h / {model.HEAVY_RAIN_MM:g} mm); multiplies the rest",
    },
    "scale": ("risk_score is a relative index from 0 to 100, not a probability. Each count enters as "
              "n / (n + typical), which is 0.5 at today's typical (median) count and never reaches 1, so no "
              "field reaches 100 and busy places keep their order (risk-v2 saturated: dozens of fields tied "
              "at 100.0). Compare scores within one run, not across runs."),
    "fields": {
        "risk_rank": "1 = the highest score of this run (of risk_rank_of scored fields); equal scores share a rank",
        "risk_percentile": "the share of this run's scored fields with a lower score, in percent",
        "risk_band": "the field's band in this run's ordering by risk_score (top 1%, 1-5%, 5-10%, 10-25%, 25-50%, "
                     "bottom 50%)",
        "burn_probability": ("calibrated on this season's burns, for the ordering the backtest could test: "
                             "neighbourhood and history only (the burn-scar table does not say when a past "
                             "burnt field was harvested, so timing is left out, and so is rain). The field is "
                             "placed in that ordering among today's fields (burn_probability_band, which can "
                             "differ from risk_band; backtest.tested_order says how closely the two orderings "
                             "agree), and burn_probability is the share of fields in that band of their pass "
                             "that burnt by the next clear Sentinel-2 pass (backtest.calibration), with "
                             "burn_probability_ci95. A rough frequency, not a forecast for the field; null "
                             "when the stored backtest is not from the run that made today's scores"),
        "burn_probability_band": "the field's band in the tested ordering (the band burn_probability is for)",
    },
    "note": "A transparent ranking, not a probability: it orders today's fields by how likely they are to burn "
            "next. See `backtest` for how well that ordering worked on this season.",
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
# Scores change only when the risk job runs (every 3 h), and a 500-field answer pulls ~0.55 MB of rows
# from the hosted database (5,000 fields: 3.3 MB): keep each answer for 10 minutes per instance.
@cached_route(ttl=600)
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
        return respond(_empty("harvest_cluster does not exist yet (the sentinel module creates it)", session), sources)
    params: dict[str, Any] = {"w": bbox[0], "s": bbox[1], "e": bbox[2], "n": bbox[3],
                              "district": district.strip() if district else None, "min_score": min_score}
    totals = session.execute(text(_FIELDS_SQL + """
        SELECT count(*), coalesce(sum(area_ha), 0), count(risk_score),
               coalesce(sum(area_ha) FILTER (WHERE risk_score IS NOT NULL), 0),
               max(risk_factors ->> 'as_of'), bool_or(risk_factors -> 'v' IS NOT NULL)
        FROM h
    """), params).one()
    if totals[0] == 0:
        any_rows = session.execute(text("SELECT EXISTS (SELECT 1 FROM harvest_cluster)")).scalar_one()
        reason = "no harvested-but-unburnt fields match these filters (bbox/district/min_score)" if any_rows \
            else "no harvested-but-unburnt fields have been mapped yet"
        return respond(_empty(reason, session), sources, max_age=60)
    rows = session.execute(text(_FIELDS_SQL + """
        SELECT id, ST_AsGeoJSON(geom, 6)::json, area_ha, harvested_after, harvested_before, last_seen_unburnt,
               risk_score, risk_factors, district_name, village_name, village_inferred,
               ST_X(centroid), ST_Y(centroid), mgrs_tile, state
        FROM h ORDER BY risk_score DESC NULLS LAST, area_ha DESC, id LIMIT :limit
    """), {**params, "limit": limit}).all()
    # Rows the job stored compactly are re-hydrated with the newest run's shared values (see
    # analytics/risk.py); rows scored before risk_run existed carry their full factors.
    run = latest_run(session) if totals[5] else None
    shared = run["factors"] if run else None
    as_of = max(filter(None, [totals[4], shared and shared.get("as_of")]), default=None)
    now = utcnow()
    backtest = _backtest_summary(session)
    calibration = _calibration_for(backtest, run)
    cuts = _tested_cuts(backtest, run) if calibration else None
    places = _ranks(session, [r[0] for r in rows if r[6] is not None])
    features = [_feature(r, now, shared, places.get(r[0]), calibration, cuts) for r in rows]
    sources = _cite_scoring_fetches(session, [f["properties"]["risk_factors"] for f in features], sources)
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
        "as_of": _parse_time(as_of),
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
        # rows stored before risk_run existed carry their model inside their factors
        "method": _method(backtest, run or _legacy_run(features)),
        "backtest": backtest,
        "reason": None if totals[2] else "fields are mapped but not scored yet (the risk job has not run)",
    }
    return respond(payload, sources, max_age=60)


def _ranks(session: Any, ids: list[int]) -> dict[int, tuple[int, int, float]]:
    """{id: (rank, scored fields, share of the run scoring at least as high)} for the given fields,
    ranked among every scored field of the run (not only those the filters let through)."""
    if not ids:
        return {}
    rows = session.execute(text("""
        SELECT id, rk, n, top FROM (
            SELECT id, rank() OVER (ORDER BY risk_score DESC) AS rk, count(*) OVER () AS n,
                   cume_dist() OVER (ORDER BY risk_score DESC) AS top
            FROM harvest_cluster WHERE risk_score IS NOT NULL
        ) AS r WHERE id = ANY(:ids)
    """), {"ids": ids}).all()
    return {int(r[0]): (int(r[1]), int(r[2]), float(r[3])) for r in rows}


def _calibration_for(backtest: dict[str, Any] | None, run: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The backtest's calibration bands, only when that backtest tested the model that made today's
    scores (a v2 backtest says nothing about v3 scores)."""
    if not backtest or not backtest.get("calibration"):
        return None
    if run is not None and backtest.get("model") not in (None, run.get("model")):
        return None
    return backtest["calibration"].get("bands") or None


def _legacy_run(features: list[dict[str, Any]]) -> dict[str, Any] | None:
    """{"model": ...} of rows scored before risk_run existed (full factors dicts), or None."""
    model_name = next((f["properties"]["risk_factors"].get("model") for f in features
                       if isinstance(f["properties"].get("risk_factors"), dict)), None)
    return {"model": model_name} if model_name else None


def _tested_cuts(backtest: dict[str, Any] | None, run: dict[str, Any] | None) -> list[float | None] | None:
    """The tested ordering's band cuts of the run that made today's scores (jobs/risk.py
    _tested_order); None when the stored backtest belongs to another run or has none."""
    tested = (backtest or {}).get("tested_order") or {}
    if not tested.get("cuts") or run is None or tested.get("run_id") != run.get("id"):
        return None
    return [c.get("min_tested_score") for c in tested["cuts"]]


def _method(backtest: dict[str, Any] | None, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """METHOD, with the note saying how strong the ranking proved, in the backtest's own numbers;
    and, while the stored scores still come from an older model, saying so."""
    method = dict(METHOD)
    stored = (run or {}).get("model")
    if stored and stored != model.MODEL_VERSION:
        method["scores_model"] = stored
        method["scale"] = (f"Today's stored scores were made by {stored}, before the scale below; the next run of "
                           f"the risk job rescores every field with {model.MODEL_VERSION}. " + METHOD["scale"])
    a = (backtest or {}).get("all_pairs") or {}
    strength, ci = (backtest or {}).get("strength"), a.get("auc_ci95") or [None, None]
    if not strength or a.get("auc_weighted_mean") is None or None in ci:
        return method
    tested = (backtest or {}).get("tested_order") or {}
    if (backtest or {}).get("timing_tested"):
        what, follow = "that ordering", ""
    else:
        what = ("the part of that ordering the backtest could test (neighbourhood and history; timing and rain "
                "cannot be reconstructed for past passes)")
        rho, overlap = tested.get("spearman_with_risk_score"), tested.get("top_1pct_overlap")
        if rho is not None and tested.get("run_id") == (run or {}).get("id"):
            follow = f" Timing is untested; today's full score follows the tested ordering at Spearman {rho:.2f}"
            follow += (f", and {overlap:.0%} of its top 1% are also in the tested top 1%." if overlap is not None
                       else ".")
        else:
            follow = " Timing is untested."
    method["note"] = (
        f"A relative ranking from 0 to 100, not a probability: it orders today's fields by how likely they are to "
        f"burn next. On this season's burns, {what} is {strength} (AUC {a['auc_weighted_mean']:.3f} over "
        f"{a['pairs']} pass pairs, 95% CI {ci[0]:.3f}-{ci[1]:.3f}; 0.5 is chance).{follow} See `backtest` for how "
        "well the ordering worked on this season.")
    return method


def _feature(r: Any, now: datetime, shared: dict[str, Any] | None, place: tuple[int, int, float] | None = None,
             calibration: list[dict[str, Any]] | None = None, cuts: list[float | None] | None = None
             ) -> dict[str, Any]:
    factors = model.expand_factors(r[7], shared, r[3], r[4])
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
            "last_seen_unburnt": r[5], "days_since_harvest": days, "risk_score": _round(r[6], 2),
            "risk_factors": factors, "district": r[8], "village": r[9], "village_inferred": bool(r[10]),
            "centroid": [_round(r[11], 6), _round(r[12], 6)], "mgrs_tile": r[13], "state": r[14],
            **_placing(place, calibration, _tested_of(factors), cuts),
        },
    }


def _tested_of(factors: dict[str, Any] | None) -> float | None:
    """The field's score in the ordering the backtest tested (analytics/risk.py tested_score)."""
    if not factors:
        return None
    neigh = (factors.get("neighbourhood") or {}).get("value")
    hist = (factors.get("history") or {}).get("value")
    return model.tested_score(None if neigh is None else float(neigh), None if hist is None else float(hist))


def _placing(place: tuple[int, int, float] | None, calibration: list[dict[str, Any]] | None,
             tested: float | None = None, cuts: list[float | None] | None = None) -> dict[str, Any]:
    """risk_rank, risk_percentile and risk_band of one field in today's ordering, and the calibrated
    burn_probability of its band in the tested ordering (None if unscored or not calibrated)."""
    if place is None:
        return {"risk_rank": None, "risk_rank_of": None, "risk_percentile": None, "risk_band": None,
                "burn_probability": None, "burn_probability_ci95": None, "burn_probability_band": None}
    rank, n, top = place
    band = model.BANDS[model.band_index(top)][1]
    index = model.tested_band(tested, cuts or [])
    tested_band = None if index is None else model.BANDS[index][1]
    cal = next((b for b in calibration or [] if tested_band and b.get("band") == tested_band), None)
    return {"risk_rank": rank, "risk_rank_of": n, "risk_percentile": round(100 * (1 - top), 3), "risk_band": band,
            "burn_probability": cal.get("burnt_share") if cal else None,
            "burn_probability_ci95": cal.get("burnt_share_ci95") if cal else None,
            "burn_probability_band": tested_band if cal else None}


def _cite_scoring_fetches(session: Any, factors_list: list[Any], sources: list[Any]) -> list[Any]:
    """Add the exact upstream calls the scores used (the rain forecast and the FIRMS archive
    pages, recorded by the risk job in risk_factors.fetch_ids) to the response's sources."""
    factors = next((f for f in factors_list if f), None) or {}
    ids = [i for group in (factors.get("fetch_ids") or {}).values() for i in (group or [])]
    seen = {s.fetch_id for s in sources}
    for fid in dict.fromkeys(ids):
        if fid in seen:
            continue
        ref = ref_for_fetch(session, fid)
        if ref is not None:
            sources.append(ref)
    return sources


def _empty(reason: str, session: Any) -> dict[str, Any]:
    return {"as_of": None, "totals": {"clusters": 0, "area_ha": 0.0, "scored": 0, "scored_area_ha": 0.0},
            "features": {"type": "FeatureCollection", "features": []}, "ranking": [],
            "method": METHOD, "backtest": _backtest_summary(session), "reason": reason}


def _backtest_summary(session: Any) -> dict[str, Any] | None:
    """The last backtest the `risk` job wrote (on its risk_run row; before that table existed, a small
    JSON file), without the per-window list, or None."""
    report = latest_backtest_summary(session)
    if report is None:
        try:
            report = orjson.loads(get_settings().cache_path(*model.BACKTEST_FILE).read_bytes())
        except (OSError, ValueError):
            return None
        report["windows_count"] = len(report.get("windows", []))
    return {"computed_at": report.get("computed_at"), "model": report.get("model"),
            "headline": report.get("headline"), "all_pairs": report.get("all_pairs"),
            "verdict": report.get("verdict"), "strength": report.get("strength"),
            "calibration": report.get("calibration"), "pairs_are": report.get("pairs_are"),
            "tested_order": report.get("tested_order"),
            "timing_tested": report.get("timing_tested"), "timing_note": report.get("timing_note"),
            "reason": report.get("reason"), "windows": report.get("windows_count", 0),
            "latest_attempt": report.get("latest_attempt"), "method": report.get("method")}


def _round(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
