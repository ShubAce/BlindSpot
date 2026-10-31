"""District field brief: where the pollution control boards' field teams (PPCB in Punjab, HSPCB in
Haryana) could look first, built from the burn scars and the burn-risk ranking.

GET /api/brief                  every district of Punjab and Haryana with candidate unseen burns in the
                                last `days` days of Sentinel-2 data, largest candidate unseen area first
GET /api/brief/{district}       one district: its villages with candidate unseen burns (a point on the
                                largest scar, case ids) and a watchlist of its villages with harvested
                                fields: those with fields in the latest risk run's top tenth first, then
                                the others by their best-ranked field (`in_top_tenth` says which)
GET /api/brief/{district}/csv   the villages as a spreadsheet, header in English, Hindi or Punjabi; the
                                count and area columns name their passes and carry the audit

Two kinds of evidence, each carrying its qualification in every answer:
- Candidate unseen burns: burn scars no NASA FIRMS detection explains (burn_cluster.unseen). A hand
  audit found most of them, not all, to be real burns (analytics/burnscar.py AUDIT), so their counts
  and areas are upper bounds; the audit precision and its 95% interval travel with them, in the CSV
  too (its header cells), because a saved spreadsheet loses everything around it.
- The watchlist: harvested fields whose straw was still lying on the latest clear pass
  (harvest_cluster), in the order of the latest risk run. A rank says where to look first, never the
  chance that a field burns; the backtest the risk job stores says how well that order worked. Many
  districts have no field in the run's top tenth: their villages are still listed, by best rank,
  with `in_top_tenth` false, and nothing here calls them top tenth.

The window ends at the newest Sentinel-2 pass in the scar table, not today. Passes reach the table a
day or more after they are taken and Sentinel-2 sees a field only every few days, so a window ending
today comes up short, or empty, whenever the data is a few days behind. Days are the UTC dates of the
pass that shows a burn, as in GET /api/scars since/until, so
/api/scars?district=<d>&unseen=true&since=<from>&until=<to> lists exactly the scars a brief counts
(passes over Punjab and Haryana are near 11:00 IST, so the IST date is the same).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response

from blindspot.analytics import risk as risk_model
from blindspot.analytics.burnscar import RULE, audit_summary
from blindspot.api import SessionDep, respond
from blindspot.api.cache import cached_route, get_or_build
from blindspot.geo import IST
from blindspot.models.risk import latest_backtest_summary, latest_run
from blindspot.provenance import SourceRef, latest_refs

router = APIRouter(prefix="/api/brief", tags=["brief"])

STATES = ("Punjab", "Haryana")
# Where the figures come from: the scars (Sentinel-2 scenes and pixels, the cropland mask, the FIRMS
# records they were matched against, the boundaries that name them), the risk run's rain forecast,
# and the village names (census 2011 village points, OpenStreetMap places).
SOURCE_KEYS = ["stac_e84", "s2_cog", "worldcover", "firms", "boundaries", "openmeteo", "census2011", "osm"]
TOP_SHARE = risk_model.BANDS[2][0]  # 0.10: the "top 1%", "top 1-5%" and "top 5-10%" bands of GET /api/risk
VILLAGE_KM = 3.0  # jobs/sentinel.py VILLAGE_MAX_KM (not imported: that module loads rasterio)
WEB_BASE_URL = "https://blindspot-web-five.vercel.app"  # the public website; /case/<id> is a scar's case file
CACHE_TTL_S = 600  # the scars and the ranks change once a day: keep a built answer 10 minutes per instance
MAX_AGE_S = 300  # browsers; the CDN keeps an answer until the DAILY clock ticks (api/cache.py CDN_RULES)
RANK_NOT_PROBABILITY = "A rank says where to look first; it is not the chance that a field will burn."

DaysQuery = Annotated[int, Query(ge=1, le=30, description="days of Sentinel-2 passes, ending at the newest pass in the data")]


# --- the window -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """The UTC days of the passes a brief counts: `days` days ending on the newest pass's day."""

    first: dt.date
    last: dt.date
    days: int
    newest_pass: dt.datetime

    @classmethod
    def ending_at(cls, newest_pass: dt.datetime, days: int) -> Window:
        last = newest_pass.astimezone(dt.UTC).date()
        return cls(last - dt.timedelta(days=days - 1), last, days, newest_pass)

    @property
    def start(self) -> dt.datetime:
        return dt.datetime.combine(self.first, dt.time.min, dt.UTC)

    @property
    def end(self) -> dt.datetime:  # exclusive: midnight after the last day
        return dt.datetime.combine(self.last + dt.timedelta(days=1), dt.time.min, dt.UTC)

    def as_json(self) -> dict[str, Any]:
        return {
            "from": self.first, "to": self.last, "days": self.days, "newest_pass": self.newest_pass,
            "anchor": "the newest Sentinel-2 pass in the burn-scar table, not today",
            "basis": "UTC dates of the pass that shows each burn, as in GET /api/scars since/until",
        }


# --- districts ------------------------------------------------------------------------------------


def known_districts(session: Session) -> list[dict[str, Any]]:
    """[{district, state, newest}] for every district of Punjab and Haryana with burn scars of the
    current model: one scan of burn_cluster, kept 10 minutes per instance. `newest` is the district's
    newest pass; the newest of them all anchors every window."""

    def build() -> list[dict[str, Any]]:
        rows = session.execute(text("""
            SELECT district, max(state) AS state, max(burn_before) AS newest
            FROM burn_cluster
            WHERE model_version = :version AND state = ANY(:states) AND district IS NOT NULL
            GROUP BY district ORDER BY district"""), {"version": RULE.version, "states": list(STATES)}).mappings()
        return [dict(r) for r in rows]

    return get_or_build("brief:districts", RULE.version, build, ttl=CACHE_TTL_S)


def _key(name: str) -> str:
    """Letters and digits only, case-folded: "S.A.S Nagar", "sas nagar" and "s-a-s-nagar" agree."""
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def resolve_district(session: Session, name: str) -> dict[str, Any] | None:
    """The stored district a name means: the same name in any case (spaces collapsed), or failing
    that the one district whose letters and digits match; None when there is no such district."""
    known = known_districts(session)
    wanted = " ".join(name.split()).casefold()
    exact = [d for d in known if d["district"].casefold() == wanted]
    if exact:
        return exact[0]
    key = _key(name)
    loose = [d for d in known if key and _key(d["district"]) == key]
    return loose[0] if len(loose) == 1 else None


def _district_or_404(session: Session, name: str) -> dict[str, Any]:
    found = resolve_district(session, name)
    if found is None:
        names = ", ".join(d["district"] for d in known_districts(session)) or "none yet (job sentinel)"
        raise HTTPException(404, f"no district named {name.strip()!r} has burn scars in Punjab or Haryana; "
                                 f"districts with burn scars: {names}")
    return found


def _newest_pass(session: Session) -> dt.datetime | None:
    return max((d["newest"] for d in known_districts(session)), default=None)


# --- what qualifies the figures -------------------------------------------------------------------


def candidate_unseen_audit() -> dict[str, Any]:
    """The hand audit of candidate unseen burns: the share found to be real burns, its 95% interval
    and how many were audited. Every count or area of candidate unseen burns comes with it."""
    audit = audit_summary()
    unseen = audit["candidate_unseen"]
    lo, hi = unseen["precision_ci95"]
    return {
        "precision": unseen["precision"], "precision_ci95": [lo, hi], "audited": unseen["audited"],
        "method": audit.get("method"), "date": audit.get("date"), "current": audit.get("current"),
        "reading": "precision = share of audited candidate unseen burns that were real burns; so every "
                   "count and area of candidate unseen burns is an upper bound",
    }


def _ranking_reliability(session: Session, run: dict[str, Any] | None) -> dict[str, Any]:
    """How well the ranking's order worked on this season's burns, in the numbers of the backtest the
    risk job stored (risk_run.backtest), with a reading in plain sentences. Never a probability for a
    field: the top tenth's lift is a share of past fields, the AUC is over all pass pairs."""
    report = latest_backtest_summary(session) or {}
    pairs = report.get("all_pairs")
    if not isinstance(pairs, dict) or pairs.get("auc_weighted_mean") is None:
        return {"available": False,
                "reading": f"No backtest of the ranking is stored yet, so how well it orders fields is not known. "
                           f"{RANK_NOT_PROBABILITY}"}
    top = pairs["top_10pct"] if isinstance(pairs.get("top_10pct"), dict) else {}
    tested, ranked = report.get("model"), (run or {}).get("model")
    said = []
    if tested and ranked and tested != ranked:
        said.append(f"The stored backtest tested {tested}; today's ranks come from {ranked}.")
    if None not in (top.get("lift"), top.get("burnt_share"), top.get("base_rate")):
        ci = top.get("lift_ci95") or []
        interval = f" (95% CI {ci[0]:.2f}-{ci[1]:.2f})" if len(ci) == 2 and None not in ci else ""
        said.append(f"In this season's backtest, fields in the top tenth of their pass burnt by the next clear pass "
                    f"{top['lift']:.2f} times as often as the average field{interval}: {top['burnt_share']:.1%} "
                    f"against {top['base_rate']:.1%}.")
    if not report.get("timing_tested"):
        said.append("The backtest could order past fields only by burning nearby and last season's fires: the "
                    "time since harvest and the rain cannot be reconstructed for past passes.")
    ci = pairs.get("auc_ci95") or []
    has_ci = len(ci) == 2 and None not in ci
    over = f" over all {pairs['pairs']} pass pairs" if pairs.get("pairs") else ""
    auc = f"{pairs['auc_weighted_mean']:.3f}"
    if report.get("strength"):
        interval = f", 95% CI {ci[0]:.3f}-{ci[1]:.3f}" if has_ci else ""
        said.append(f"Checked{over}, the ranking is {report['strength']} (AUC {auc}{interval}; 0.5 is chance).")
    else:
        interval = f" (95% CI {ci[0]:.3f}-{ci[1]:.3f})" if has_ci else ""
        said.append(f"Checked{over}, the ranking's AUC is {auc}{interval}; 0.5 is chance.")
    said.append(RANK_NOT_PROBABILITY)
    return {
        "available": True,
        "model": tested,
        "computed_at": report.get("computed_at"),
        "strength": report.get("strength"),
        "timing_tested": bool(report.get("timing_tested")),
        "all_pairs": {
            "pairs": pairs.get("pairs"),
            "auc_weighted_mean": pairs.get("auc_weighted_mean"),
            "auc_ci95": pairs.get("auc_ci95"),
            "top_10pct": {k: top.get(k) for k in ("burnt_share", "burnt_share_ci95", "lift", "lift_ci95", "base_rate")}
            if top else None,
        },
        "reading": " ".join(said),
    }


def _risk_run(run: dict[str, Any] | None, fields_ranked: int | None) -> dict[str, Any]:
    return {
        "run_id": (run or {}).get("id"),
        "model": (run or {}).get("model"),
        "as_of": (run or {}).get("as_of"),
        "fields_ranked": fields_ranked,
        "top_tenth": f"a field is in the top tenth when at most {TOP_SHARE:.0%} of the run's ranked fields score as "
                     "high or higher (the top 1%, 1-5% and 5-10% bands of GET /api/risk)",
    }


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _notes(win: Window, audit: dict[str, Any], run: dict[str, Any] | None,
           reliability: dict[str, Any]) -> list[str]:
    """The qualifications of every figure in a brief, as plain sentences for the page."""
    lo, hi = audit["precision_ci95"]
    days = f"{win.days} day" + ("" if win.days == 1 else "s")
    as_of = (run or {}).get("as_of")
    by = f"the burn-risk run of {as_of.astimezone(IST):%Y-%m-%d %H:%M} IST" if as_of else "the latest burn-risk run"
    return [
        ("Candidate unseen burns are Sentinel-2 burn scars that no NASA FIRMS fire detection explains. They are "
         f"candidates, not confirmed fires: in a hand audit of {audit['audited']} of them, {_pct(audit['precision'])} "
         f"were real burns (95% CI {_pct(lo)}-{_pct(hi)}), so every count and area of them here is an upper bound."),
        (f"The window is the {days} of Sentinel-2 passes up to the newest one in the data ({win.last}), not up to "
         "today: Sentinel-2 sees a field only every few days, and its passes are processed once a day."),
        ("Each village's point is its largest candidate unseen burn, so its map link leads to the burnt field "
         f"itself. Village names are the nearest census village within {VILLAGE_KM:g} km of a scar; scars with no "
         "village that close are listed by sub-district, without a name."),
        ("The watchlist orders harvested fields whose straw was still lying on the latest clear pass by "
         f"{by}: villages with fields in the run's top tenth first, then the others by their best-ranked "
         f"field, so a district with no field in the top tenth still gets a list. {reliability['reading']}"),
        ("These lists say where to look, not who burnt: a satellite flag needs a visit on the ground before any "
         "action, and the brief names villages only, never people."),
    ]


def maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def case_url(case_id: int) -> str:
    return f"{WEB_BASE_URL}/case/{case_id}"


def _links(district: str, days: int) -> dict[str, str]:
    name = quote(district, safe="")
    return {"brief": f"/api/brief/{name}?days={days}", "csv": f"/api/brief/{name}/csv?days={days}"}


def _day(value: dt.datetime) -> dt.date:
    return value.astimezone(dt.UTC).date()


# --- GET /api/brief -------------------------------------------------------------------------------

_UNSEEN_BY_DISTRICT = text("""
    SELECT district, max(state) AS state, count(*) AS clusters, sum(area_ha) AS area_ha,
           count(DISTINCT (village, subdistrict)) FILTER (WHERE village IS NOT NULL) AS villages,
           max(burn_before) AS newest
    FROM burn_cluster
    WHERE model_version = :version AND unseen AND state = ANY(:states) AND district IS NOT NULL
      AND burn_before >= :start AND burn_before < :end
    GROUP BY district
    ORDER BY sum(area_ha) DESC, district""")

# Every scored field is ranked among all scored fields of the run, as GET /api/risk ranks them, so the
# window runs over the whole table; unscored fields (a tile rebuilt since the last risk run) form a
# partition of their own and get no rank.
_RANKED = """
    SELECT id, district, village, subdistrict, area_ha, centroid,
           CASE WHEN risk_score IS NOT NULL THEN rank() OVER w END AS rk,
           CASE WHEN risk_score IS NOT NULL THEN cume_dist() OVER w END AS top,
           CASE WHEN risk_score IS NOT NULL THEN count(*) OVER (PARTITION BY risk_score IS NULL) END AS n
    FROM harvest_cluster
    WINDOW w AS (PARTITION BY risk_score IS NULL ORDER BY risk_score DESC)
"""

_HARVEST_BY_DISTRICT = text(f"""
    SELECT district, count(*) AS fields, sum(area_ha) AS area_ha, count(rk) AS ranked,
           count(*) FILTER (WHERE top <= :share) AS top_fields,
           count(DISTINCT (village, subdistrict)) FILTER (WHERE top <= :share AND village IS NOT NULL) AS top_villages
    FROM ({_RANKED}) AS h
    GROUP BY district""")


def district_list(session: Session, days: int) -> tuple[dict[str, Any], list[SourceRef]]:
    """Every district with candidate unseen burns in the window, largest candidate unseen area first,
    with its harvested fields and how many of its villages hold fields in the risk run's top tenth."""
    sources = latest_refs(session, SOURCE_KEYS)
    audit = candidate_unseen_audit()
    run = latest_run(session)
    newest = _newest_pass(session)
    if newest is None:
        return {"window": None, "label": "candidate unseen burns", "districts": [],
                "totals": {"districts": 0, "unseen_clusters": 0, "unseen_area_ha": 0.0},
                "candidate_unseen_audit": audit, "risk_run": _risk_run(run, None),
                "reason": "no burn scars mapped yet in Punjab or Haryana (job sentinel)"}, sources
    win = Window.ending_at(newest, days)
    unseen = session.execute(_UNSEEN_BY_DISTRICT, {"version": RULE.version, "states": list(STATES),
                                                   "start": win.start, "end": win.end}).mappings().all()
    harvest = {r["district"]: r for r in session.execute(
        _HARVEST_BY_DISTRICT, {"share": TOP_SHARE + 1e-12}).mappings()}
    districts = []
    for r in unseen:
        h = harvest.get(r["district"]) or {}
        districts.append({
            "district": r["district"], "state": r["state"],
            "unseen_clusters": r["clusters"], "unseen_area_ha": round(float(r["area_ha"]), 2),
            "villages": r["villages"], "newest_pass": _day(r["newest"]),
            "harvested_fields": h.get("fields", 0), "harvested_area_ha": round(float(h.get("area_ha") or 0), 2),
            "top_tenth_fields": h.get("top_fields", 0), "top_tenth_villages": h.get("top_villages", 0),
            "links": _links(r["district"], days),
        })
    reliability = _ranking_reliability(session, run)
    payload = {
        "window": win.as_json(),
        "label": "candidate unseen burns",
        "districts": districts,
        "districts_order": "largest candidate unseen area first",
        "totals": {"districts": len(districts), "unseen_clusters": sum(d["unseen_clusters"] for d in districts),
                   "unseen_area_ha": round(sum(float(r["area_ha"]) for r in unseen), 2)},
        "candidate_unseen_audit": audit,
        "risk_run": _risk_run(run, sum(h["ranked"] for h in harvest.values()) or None),
        "ranking_reliability": reliability,
        "notes": _notes(win, audit, run, reliability),
        "reason": None if districts else (f"no candidate unseen burns in Punjab or Haryana between {win.first} "
                                          f"and {win.last}"),
    }
    return payload, sources


# --- GET /api/brief/{district} ---------------------------------------------------------------------

_VILLAGES = text("""
    SELECT village, subdistrict, count(*) AS clusters, sum(area_ha) AS area_ha, max(burn_before) AS newest,
           (array_agg(id ORDER BY area_ha DESC, id))[1:5] AS case_ids,
           (array_agg(ST_Y(centroid) ORDER BY area_ha DESC, id))[1] AS lat,
           (array_agg(ST_X(centroid) ORDER BY area_ha DESC, id))[1] AS lon
    FROM burn_cluster
    WHERE model_version = :version AND unseen AND district = :district AND state = :state
      AND burn_before >= :start AND burn_before < :end
    GROUP BY village, subdistrict
    ORDER BY sum(area_ha) DESC, count(*) DESC, village NULLS LAST, subdistrict NULLS LAST""")

_WATCHLIST = text(f"""
    SELECT village, subdistrict, count(*) AS fields, sum(area_ha) AS area_ha,
           count(*) FILTER (WHERE top <= :share) AS top_fields,
           coalesce(sum(area_ha) FILTER (WHERE top <= :share), 0) AS top_area_ha,
           min(rk) AS best_rank, max(n) AS ranked,
           (array_agg(id ORDER BY rk NULLS LAST, area_ha DESC, id))[1:5] AS field_ids,
           (array_agg(ST_Y(centroid) ORDER BY rk NULLS LAST, area_ha DESC, id))[1] AS lat,
           (array_agg(ST_X(centroid) ORDER BY rk NULLS LAST, area_ha DESC, id))[1] AS lon
    FROM ({_RANKED}) AS h
    WHERE district = :district
    GROUP BY village, subdistrict
    ORDER BY count(*) FILTER (WHERE top <= :share) DESC, min(rk) NULLS LAST, sum(area_ha) DESC,
             village NULLS LAST, subdistrict NULLS LAST""")


@dataclass
class _Villages:
    """One district's candidate unseen burns in a window, one row per (village, sub-district)."""

    district: dict[str, Any]
    window: Window
    rows: list[dict[str, Any]]
    clusters: int
    area_ha: float  # summed before rounding, so it equals the district's row in GET /api/brief


def district_villages(session: Session, name: str, days: int) -> _Villages:
    """The villages with candidate unseen burns of the district `name` means (404 if none), largest
    area first; each with a point on its largest scar, that scar's case file and a map link."""
    district = _district_or_404(session, name)
    win = Window.ending_at(_newest_pass(session) or district["newest"], days)
    found = session.execute(_VILLAGES, {"version": RULE.version, "district": district["district"],
                                        "state": district["state"], "start": win.start, "end": win.end}).mappings().all()
    rows = []
    for r in found:
        lat, lon = round(float(r["lat"]), 6), round(float(r["lon"]), 6)
        rows.append({
            "village": r["village"], "subdistrict": r["subdistrict"],
            "unseen_clusters": r["clusters"], "unseen_area_ha": round(float(r["area_ha"]), 2),
            "newest_pass": _day(r["newest"]), "lat": lat, "lon": lon, "case_ids": list(r["case_ids"]),
            "maps_url": maps_url(lat, lon), "case_url": case_url(r["case_ids"][0]),
        })
    return _Villages(district, win, rows, sum(r["clusters"] for r in found),
                     sum(float(r["area_ha"]) for r in found))


def district_brief(session: Session, name: str, days: int, limit: int | None = None
                   ) -> tuple[dict[str, Any], list[SourceRef]]:
    """One district's brief (payload, sources). `limit` caps both lists (None: every village); the
    totals always cover them all. 404 for a district with no burn scars in Punjab or Haryana."""
    v = district_villages(session, name, days)
    name, win = v.district["district"], v.window
    found = session.execute(_WATCHLIST, {"district": name, "share": TOP_SHARE + 1e-12}).mappings().all()
    watchlist = []
    for r in found:
        lat, lon = round(float(r["lat"]), 6), round(float(r["lon"]), 6)
        watchlist.append({
            "village": r["village"], "subdistrict": r["subdistrict"],
            "fields": r["fields"], "area_ha": round(float(r["area_ha"]), 2),
            "top_tenth_fields": r["top_fields"], "top_tenth_area_ha": round(float(r["top_area_ha"]), 2),
            # false: no field of this village is in the run's top tenth; it is listed by best rank only
            "in_top_tenth": r["top_fields"] > 0,
            "best_rank": r["best_rank"], "lat": lat, "lon": lon, "field_ids": list(r["field_ids"]),
            "maps_url": maps_url(lat, lon),
        })
    run = latest_run(session)
    audit = candidate_unseen_audit()
    reliability = _ranking_reliability(session, run)
    payload = {
        "district": name,
        "state": v.district["state"],
        "window": win.as_json(),
        "label": "candidate unseen burns",
        "totals": {
            "unseen_clusters": v.clusters,
            "unseen_area_ha": round(v.area_ha, 2),
            "villages": sum(1 for r in v.rows if r["village"]),
            "newest_pass": max((r["newest_pass"] for r in v.rows), default=None),
            "harvested_fields": sum(r["fields"] for r in found),
            "harvested_area_ha": round(sum(float(r["area_ha"]) for r in found), 2),
            "top_tenth_fields": sum(r["top_fields"] for r in found),
            "top_tenth_villages": sum(1 for r in found if r["village"] and r["top_fields"]),
        },
        "villages": v.rows[:limit],
        "villages_total": len(v.rows),
        "villages_order": "largest candidate unseen area first; one row per village and sub-district",
        "watchlist": watchlist[:limit],
        "watchlist_total": len(watchlist),
        # rows (named villages, and a sub-district's fields with no census village close by) with at
        # least one field in the top tenth; they come first, so watchlist[:this] are all of them
        "watchlist_in_top_tenth": sum(1 for w in watchlist if w["in_top_tenth"]),
        "watchlist_order": "villages with the most fields in the risk run's top tenth first, then by their "
                           "best-ranked field (rank 1 = the highest of the run); a row with in_top_tenth "
                           "false has no field in the top tenth and is listed by its best rank only",
        "candidate_unseen_audit": audit,
        "risk_run": _risk_run(run, max((r["ranked"] for r in found if r["ranked"] is not None), default=None)),
        "ranking_reliability": reliability,
        "notes": _notes(win, audit, run, reliability),
        "links": _links(name, days),
        "reason": None if v.rows else f"no candidate unseen burns in {name} between {win.first} and {win.last}",
    }
    return payload, latest_refs(session, SOURCE_KEYS)


# --- the CSV --------------------------------------------------------------------------------------

Lang = Literal["en", "hi", "pa"]
# A saved spreadsheet loses the page around it, so the count and area columns carry their own
# qualification: the passes they cover ({first}, {last}) and the hand audit ({audit}, CSV_AUDIT),
# which makes every figure under them an upper bound. The file stays a plain table.
# "Candidate unseen burns" and the audit in the words the bulletin and the answer check use
# (agent/verifier.py audit_sentence, unseen_sentence).
CSV_HEADER: dict[str, tuple[str, ...]] = {
    "en": ("village", "subdistrict", "district",
           "candidate unseen burns, Sentinel-2 passes {first} to {last} ({audit})",
           "area of candidate unseen burns, ha ({audit})", "newest pass date",
           "lat", "lon", "Google Maps link", "case file link"),
    "hi": ("गाँव", "तहसील", "ज़िला",
           "संभावित अनदेखी आग, उपग्रह चित्र {first} से {last} तक ({audit})",
           "संभावित अनदेखी आग का क्षेत्रफल, हेक्टेयर ({audit})", "नवीनतम उपग्रह चित्र की तारीख",
           "अक्षांश", "देशांतर", "गूगल मैप्स लिंक", "केस फ़ाइल लिंक"),
    "pa": ("ਪਿੰਡ", "ਤਹਿਸੀਲ", "ਜ਼ਿਲ੍ਹਾ",
           "ਸੰਭਾਵੀ ਅਣਦੇਖੀਆਂ ਅੱਗਾਂ, ਉਪਗ੍ਰਹਿ ਤਸਵੀਰਾਂ {first} ਤੋਂ {last} ਤੱਕ ({audit})",
           "ਸੰਭਾਵੀ ਅਣਦੇਖੀਆਂ ਅੱਗਾਂ ਦਾ ਰਕਬਾ, ਹੈਕਟੇਅਰ ({audit})", "ਨਵੀਨਤਮ ਉਪਗ੍ਰਹਿ ਤਸਵੀਰ ਦੀ ਮਿਤੀ",
           "ਅਕਸ਼ਾਂਸ਼", "ਰੇਖਾਂਸ਼", "ਗੂਗਲ ਮੈਪਸ ਲਿੰਕ", "ਕੇਸ ਫ਼ਾਈਲ ਲਿੰਕ"),
}
CSV_AUDIT: dict[str, str] = {
    "en": "upper bound: in a hand audit of {n}, {p} were real burns, 95% CI {lo}-{hi}",
    "hi": "ऊपरी सीमा: {n} संभावित मामलों की जाँच में {p} सचमुच जले हुए निकले, 95% CI {lo}-{hi}",
    "pa": "ਉਪਰਲੀ ਹੱਦ: {n} ਸੰਭਾਵੀ ਮਾਮਲਿਆਂ ਦੀ ਜਾਂਚ ਵਿੱਚ {p} ਸੱਚਮੁੱਚ ਸੜੇ ਹੋਏ ਨਿਕਲੇ, 95% CI {lo}-{hi}",
}
# Excel and LibreOffice run a cell that starts with one of these as a formula (CSV injection).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
UNNAMED: dict[str, str] = {
    "en": f"unnamed (no census village within {VILLAGE_KM:g} km)",
    "hi": f"अनाम ({VILLAGE_KM:g} किमी के भीतर कोई जनगणना गाँव नहीं)",
    "pa": f"ਬੇਨਾਮ ({VILLAGE_KM:g} ਕਿਮੀ ਦੇ ਅੰਦਰ ਕੋਈ ਜਨਗਣਨਾ ਪਿੰਡ ਨਹੀਂ)",
}


def csv_filename(district: str, first: dt.date, last: dt.date) -> str:
    """blindspot-brief-<district>-<first day>-to-<last day>.csv: the district as a plain ASCII slug
    and the window's days, so a saved file still says which passes it counts."""
    slug = re.sub(r"[^a-z0-9]+", "-", district.casefold()).strip("-") or "district"
    return f"blindspot-brief-{slug}-{first.isoformat()}-to-{last.isoformat()}.csv"


def csv_header(lang: Lang, audit: dict[str, Any], win: Window) -> list[str]:
    """The header row in `lang`, with the window's days and the audit (candidate_unseen_audit) in
    the count and area columns."""
    lo, hi = audit["precision_ci95"]
    qualification = CSV_AUDIT[lang].format(n=audit["audited"], p=_pct(audit["precision"]), lo=_pct(lo), hi=_pct(hi))
    return [cell.format(first=win.first, last=win.last, audit=qualification) for cell in CSV_HEADER[lang]]


def text_cell(value: str) -> str:
    """A name as text, never a formula. Village names include OpenStreetMap places, which anyone can
    edit, and this file is made to be opened in Excel; a leading apostrophe keeps a name that starts
    like a formula as text (OWASP's advice on CSV injection)."""
    return "'" + value if value.startswith(FORMULA_START) else value


def villages_csv(v: _Villages, lang: Lang, audit: dict[str, Any]) -> bytes:
    """One row per village, header in `lang`, numbers with the digits 0-9, as UTF-8 with a byte-order
    mark: without it Excel reads the file in the machine's code page and garbles Hindi and Punjabi."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(csv_header(lang, audit, v.window))
    for r in v.rows:
        writer.writerow([text_cell(r["village"] or UNNAMED[lang]), text_cell(r["subdistrict"] or ""),
                         text_cell(v.district["district"]),
                         r["unseen_clusters"], f"{r['unseen_area_ha']:.2f}", r["newest_pass"].isoformat(),
                         f"{r['lat']:.6f}", f"{r['lon']:.6f}", r["maps_url"], r["case_url"]])
    return ("﻿" + out.getvalue()).encode("utf-8")


# --- routes ---------------------------------------------------------------------------------------
# The CDN keeps every 200 answer of /api/brief* until the DAILY clock of api/cache.py ticks (CDN_RULES),
# the clock of /api/scars and /api/risk, whose daily Sentinel-2 and risk runs feed the brief; an
# error is never kept. In process, a finished answer is kept CACHE_TTL_S per instance.


@router.get("")
@cached_route(ttl=CACHE_TTL_S)
def brief_index(session: SessionDep, days: DaysQuery = 7) -> Response:
    """Every district of Punjab and Haryana with candidate unseen burns in the last `days` days of
    data, largest candidate unseen area first, with its harvested fields and the villages holding
    fields in the risk run's top tenth. Every answer carries the audit that qualifies the burns."""
    payload, sources = district_list(session, days)
    return respond(payload, sources, max_age=MAX_AGE_S)


@router.get("/{district}")
@cached_route(ttl=CACHE_TTL_S)
def brief_district(
    session: SessionDep,
    district: Annotated[str, Path(max_length=80, description="as stored in burn_cluster.district, any case")],
    days: DaysQuery = 7,
    limit: Annotated[int, Query(ge=1, le=2000, description="rows per list; the totals cover every village")] = 50,
) -> Response:
    """One district: villages with candidate unseen burns in the window (count, hectares, newest pass,
    a point on the largest scar, case ids) and the watchlist of its villages with harvested fields,
    those with fields in the latest risk run's top tenth first (`in_top_tenth`), with the audit, the
    backtest's reading and the notes."""
    payload, sources = district_brief(session, district, days, limit)
    return respond(payload, sources, max_age=MAX_AGE_S)


@router.get("/{district}/csv")
@cached_route(ttl=CACHE_TTL_S)
def brief_district_csv(
    session: SessionDep,
    district: Annotated[str, Path(max_length=80)],
    days: DaysQuery = 7,
    lang: Lang = "en",
) -> Response:
    """The district's villages with candidate unseen burns as a CSV for a spreadsheet: one row per
    village, a Google Maps link to its largest scar and that scar's case file on the website. The
    header names the window and the audit; `Link` points to the JSON brief, whose `sources` list
    where every figure comes from."""
    v = district_villages(session, district, days)
    name = v.district["district"]
    filename = csv_filename(name, v.window.first, v.window.last)
    return Response(villages_csv(v, lang, candidate_unseen_audit()), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Link": f'<{_links(name, days)["brief"]}>; rel="describedby"',
                             "Cache-Control": f"public, max-age={MAX_AGE_S}"})
