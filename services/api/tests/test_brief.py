"""Tests of the district field brief (api/routers/brief.py) and the investigator's get_field_brief tool.

The fixture writes burn scars and harvested fields of two districts no real row uses ("T.B. Nagar" in
Punjab, "Test Brief Haryana") for one test tile, placed in the Indian Ocean so no map query's box holds
them, plus one risk run carrying a backtest (its all-pairs numbers are the ones measured on the dev
database and written in docs/api-contract-notes.md) and one fetch record, all in the *_test database.
Afterwards it deletes exactly the rows it inserted, by id. Gemini is never called.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.agent import prompts, runner, tools
from blindspot.agent.verifier import (
    build_evidence,
    check_honesty,
    extract_claims,
    foreign_script,
    honesty_facts,
    verify,
)
from blindspot.analytics import risk as risk_model
from blindspot.analytics.burnscar import RULE, audit_summary
from blindspot.api.cache import DAILY, cdn_rule
from blindspot.api.routers import brief
from blindspot.db import init_db, session_scope
from blindspot.provenance import label_for, record_fetch

TILE = "TBRF1"  # an mgrs_tile value no real row uses
A, B = "T.B. Nagar", "Test Brief Haryana"
NEWEST = datetime(2026, 10, 30, 5, 40, tzinfo=UTC)  # the newest pass of the fixture: every window ends here
RUN_AS_OF = datetime(2026, 10, 31, 8, 10, tzinfo=UTC)  # 13:40 IST


def _t(day: int, hour: int = 5, minute: int = 40) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


# key, village, subdistrict, district, unseen, area_ha, burn_before (the pass that shows the burn)
SCARS = [
    ("a1", "Alpha", "North", A, True, 3.0, NEWEST),
    ("a2", "Alpha", "North", A, True, 5.0, _t(28)),  # Alpha's largest: its point and first case id
    ("a3", "Alpha", "South", A, True, 1.0, _t(25)),  # the same name in another sub-district
    ("b1", "Beta", "North", A, True, 2.0, _t(24, 0, 30)),  # half an hour into a 7-day window
    ("b2", "Beta", "North", A, False, 9.0, _t(29)),  # a FIRMS detection explains it: not a candidate
    ("b3", "Beta", "North", A, None, 7.0, _t(29)),  # no fire-record verdict yet: not a candidate
    ("g1", "Gamma", "North", A, True, 4.0, _t(23, 23, 50)),  # ten minutes before a 7-day window
    ("n1", None, "North", A, True, 1.5, _t(27)),  # no census village within 3 km
    ("d1", "Delta", "West", B, True, 2.5, _t(26)),
]
STATE = {A: "Punjab", B: "Haryana"}

# Harvested fields: four at the top of the run (the top tenth of 30 ranked fields is ranks 1-3), 26
# far below them, and one the risk job has not scored yet.
TOP = [("Beta", "North", A, 99.9), ("Alpha", "North", A, 99.8), ("Alpha", "North", A, 99.7),
       ("Alpha", "South", A, 99.6)]
LOW = ([("Alpha", "North", A)] * 2 + [("Beta", "North", A)] * 3 + [("Zeta", "East", A)] * 5
       + [(None, "North", A)] * 2 + [("Delta", "West", B)] * 14)
FIELDS = TOP + [(*f, 50.0 - i) for i, f in enumerate(LOW)] + [("Alpha", "North", A, None)]

BACKTEST = {
    "computed_at": "2026-10-31T08:10:00Z", "model": risk_model.MODEL_VERSION, "strength": "weak",
    "timing_tested": False, "verdict": "The ranking is weak.", "windows": [],
    "all_pairs": {"pairs": 148, "auc_weighted_mean": 0.56, "auc_ci95": [0.491, 0.663],
                  "top_10pct": {"burnt_share": 0.216, "lift": 1.42, "lift_ci95": [1.24, 1.73], "base_rate": 0.152}},
}

_SCAR_SQL = text("""
    INSERT INTO burn_cluster (geom, centroid, area_ha, n_px, res_m, mgrs_tile, scope, block, burn_after,
        burn_before, gap_days, scene_pre, scene_post, collection, hazy, confidence, unseen, state, district,
        subdistrict, village, model_version)
    VALUES (ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 0.001),
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :area, 25, 20, :tile, 'test', 'test', :after, :before,
            5, 'test-pre', 'test-post', 'sentinel-2-c1-l2a', false, 0.9, :unseen, :state, :district,
            :subdistrict, :village, :version)
    RETURNING id""")
_FIELD_SQL = text("""
    INSERT INTO harvest_cluster (geom, centroid, area_ha, n_px, res_m, mgrs_tile, scope, harvested_after,
        harvested_before, last_seen_unburnt, scene_last, collection, state, district, subdistrict, village,
        risk_score, model_version)
    VALUES (ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 0.001),
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :area, 25, 20, :tile, 'test', :after, :before, :seen,
            'test', 'sentinel-2-c1-l2a', :state, :district, :subdistrict, :village, :score, 'test-brief')
    RETURNING id""")


def _field_area(i: int) -> float:
    return 1.0 + 0.25 * (i % 4)


@pytest.fixture(scope="module")
def rows() -> Any:
    """Insert the fixture's scars, fields, risk run and fetch record; delete exactly them afterwards."""
    init_db()
    scar_ids: dict[str, int] = {}
    field_ids: list[int] = []
    run_id = fetch_id = None
    try:
        with session_scope() as s:
            for i, (key, village, sub, district, unseen, area, before) in enumerate(SCARS):
                scar_ids[key] = s.execute(_SCAR_SQL, {
                    "lon": 65.0 + i / 100, "lat": -8.0, "area": area, "tile": TILE,
                    "after": before - timedelta(days=5), "before": before, "unseen": unseen,
                    "state": STATE[district], "district": district, "subdistrict": sub, "village": village,
                    "version": RULE.version}).scalar_one()
            for i, (village, sub, district, score) in enumerate(FIELDS):
                field_ids.append(s.execute(_FIELD_SQL, {
                    "lon": 65.5 + i / 100, "lat": -8.0, "area": _field_area(i), "tile": TILE,
                    "after": _t(20), "before": _t(25), "seen": NEWEST, "state": STATE[district],
                    "district": district, "subdistrict": sub, "village": village, "score": score}).scalar_one())
            run_id = s.execute(text(
                "INSERT INTO risk_run (as_of, model, fields, factors, backtest) VALUES "
                "(:t, :m, :n, CAST('{}' AS jsonb), CAST(:b AS jsonb)) RETURNING id"),
                {"t": RUN_AS_OF, "m": risk_model.MODEL_VERSION, "n": len(FIELDS) - 1,
                 "b": json.dumps(BACKTEST)}).scalar_one()
        fetch_id = record_fetch("s2_cog", "https://earth-search.aws.element84.com/v1/collections/"
                                "sentinel-2-c1-l2a/items/test-brief", ok=True, started_at=datetime.now(UTC))
        yield {"scars": scar_ids, "fields": field_ids, "run": run_id, "fetch": fetch_id}
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM burn_cluster WHERE id = ANY(:ids) AND mgrs_tile = :t"),
                      {"ids": list(scar_ids.values()), "t": TILE})
            s.execute(text("DELETE FROM harvest_cluster WHERE id = ANY(:ids) AND mgrs_tile = :t"),
                      {"ids": field_ids, "t": TILE})
            if run_id is not None:
                s.execute(text("DELETE FROM risk_run WHERE id = :id"), {"id": run_id})
            if fetch_id is not None:
                s.execute(text("DELETE FROM source_fetch WHERE id = :id"), {"id": fetch_id})


@pytest.fixture(scope="module")
def client(rows: Any) -> Any:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def _ranked_fields() -> int:
    with session_scope() as s:
        return s.execute(text("SELECT count(*) FROM harvest_cluster WHERE risk_score IS NOT NULL")).scalar_one()


def _area(district: str, keys: set[str] | None = None) -> float:
    return sum(a for k, _, _, d, u, a, _ in SCARS if d == district and u and (keys is None or k in keys))


AUDIT = audit_summary()["candidate_unseen"]


# --- GET /api/brief -------------------------------------------------------------------------------


def test_list_has_the_window_the_districts_and_their_qualification(client: TestClient, rows: Any) -> None:
    r = client.get("/api/brief", params={"days": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    w = body["window"]
    assert (w["from"], w["to"], w["days"]) == ("2026-10-24", "2026-10-30", 7)
    assert w["newest_pass"] == "2026-10-30T05:40:00Z" and "not today" in w["anchor"]
    ours = [d for d in body["districts"] if d["district"] in (A, B)]
    assert [d["district"] for d in ours] == [A, B]  # largest candidate unseen area first
    a, b = ours
    in_window = {"a1", "a2", "a3", "b1", "n1"}
    assert a["state"] == "Punjab" and a["unseen_clusters"] == 5 and a["unseen_area_ha"] == _area(A, in_window) == 12.5
    assert a["villages"] == 3  # Alpha/North, Alpha/South, Beta/North; the unnamed scar is no village
    assert a["newest_pass"] == "2026-10-30"
    assert a["harvested_fields"] == 17 and a["top_tenth_fields"] == 3 and a["top_tenth_villages"] == 2
    assert a["harvested_area_ha"] == round(sum(_field_area(i) for i, f in enumerate(FIELDS) if f[2] == A), 2)
    assert (b["state"], b["unseen_clusters"], b["unseen_area_ha"], b["villages"], b["newest_pass"]) == \
        ("Haryana", 1, 2.5, 1, "2026-10-26")
    assert b["harvested_fields"] == 14 and b["top_tenth_fields"] == 0 and b["top_tenth_villages"] == 0
    assert a["links"] == {"brief": "/api/brief/T.B.%20Nagar?days=7", "csv": "/api/brief/T.B.%20Nagar/csv?days=7"}
    # the audit always travels with the figures
    audit = body["candidate_unseen_audit"]
    assert audit["precision"] == AUDIT["precision"] and audit["precision_ci95"] == list(AUDIT["precision_ci95"])
    assert audit["audited"] == AUDIT["audited"] == 23
    assert body["label"] == "candidate unseen burns"
    assert "candidates, not confirmed fires" in body["notes"][0] and "87%" in body["notes"][0]
    assert "95% CI 68%-96%" in body["notes"][0] and "upper bound" in body["notes"][0]
    assert {s["key"] for s in body["sources"]} >= set(brief.SOURCE_KEYS)
    # on the DAILY clock of api/cache.py (the clock of /api/scars and /api/risk), not the hourly one
    cc = r.headers["cache-control"]
    assert re.search(r"s-maxage=\d+", cc) and "stale-while-revalidate=172800" in cc, cc
    for path in ("/api/brief", f"/api/brief/{A}", f"/api/brief/{A}/csv"):
        assert cdn_rule(path).clock is DAILY, path


def test_the_window_ends_at_the_newest_pass_not_today(client: TestClient, rows: Any) -> None:
    """Sentinel-2 data runs days behind: a window ending today would hold none of the newest scars."""
    one = client.get("/api/brief", params={"days": 1}).json()
    assert (one["window"]["from"], one["window"]["to"]) == ("2026-10-30", "2026-10-30")
    assert one["window"]["to"] < datetime.now(UTC).date().isoformat()  # the newest pass is in the past
    ours = {d["district"]: d for d in one["districts"] if d["district"] in (A, B)}
    assert list(ours) == [A] and ours[A]["unseen_clusters"] == 1 and ours[A]["unseen_area_ha"] == 3.0
    month = client.get("/api/brief", params={"days": 30}).json()
    assert month["window"]["from"] == "2026-10-01"
    a = next(d for d in month["districts"] if d["district"] == A)
    assert a["unseen_clusters"] == 6 and a["unseen_area_ha"] == _area(A) == 16.5 and a["villages"] == 4
    for bad in (0, 31):
        assert client.get("/api/brief", params={"days": bad}).status_code == 422
    w = brief.Window.ending_at(datetime(2026, 10, 30, 23, 59, tzinfo=UTC), 7)
    assert (w.first.isoformat(), w.last.isoformat()) == ("2026-10-24", "2026-10-30")
    assert w.end - w.start == timedelta(days=7)


# --- GET /api/brief/{district} ---------------------------------------------------------------------


def test_district_brief_lists_villages_and_the_watchlist(client: TestClient, rows: Any) -> None:
    ids = rows["scars"]
    r = client.get("/api/brief/t.b. nagar", params={"days": 7})  # any case
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["district"] == A and body["state"] == "Punjab" and body["reason"] is None
    assert client.get("/api/brief/TB-Nagar").json()["district"] == A  # letters and digits are enough
    got = [(v["village"], v["subdistrict"], v["unseen_clusters"], v["unseen_area_ha"], v["newest_pass"])
           for v in body["villages"]]
    assert got == [("Alpha", "North", 2, 8.0, "2026-10-30"), ("Beta", "North", 1, 2.0, "2026-10-24"),
                   (None, "North", 1, 1.5, "2026-10-27"), ("Alpha", "South", 1, 1.0, "2026-10-25")]
    alpha = body["villages"][0]
    assert alpha["case_ids"] == [ids["a2"], ids["a1"]]  # largest first
    assert (alpha["lat"], alpha["lon"]) == (-8.0, 65.01)  # the largest scar's centroid
    assert alpha["maps_url"] == "https://www.google.com/maps/search/?api=1&query=-8.000000,65.010000"
    assert alpha["case_url"] == f"https://blindspot-web-five.vercel.app/case/{ids['a2']}"
    # the totals are the district's row of GET /api/brief
    row = next(d for d in client.get("/api/brief", params={"days": 7}).json()["districts"] if d["district"] == A)
    for key in ("unseen_clusters", "unseen_area_ha", "villages", "newest_pass", "harvested_fields",
                "harvested_area_ha", "top_tenth_fields", "top_tenth_villages"):
        assert body["totals"][key] == row[key], key
    # watchlist: most fields in the run's top tenth first, then the best-ranked field
    watch = [(w["village"], w["subdistrict"], w["fields"], w["top_tenth_fields"]) for w in body["watchlist"]]
    assert watch[:3] == [("Alpha", "North", 5, 2), ("Beta", "North", 4, 1), ("Alpha", "South", 1, 0)]
    assert [w["best_rank"] for w in body["watchlist"][:3]] == [2, 1, 4]
    assert [w["village"] for w in body["watchlist"][3:]] == ["Zeta", None]
    assert body["watchlist_total"] == 5 and body["villages_total"] == 4
    # the villages after the top tenth are listed by best rank, and flagged so nobody calls them top tenth
    assert [w["in_top_tenth"] for w in body["watchlist"]] == [True, True, False, False, False]
    assert body["watchlist_in_top_tenth"] == 2 and "in_top_tenth false" in body["watchlist_order"]
    assert body["risk_run"]["run_id"] == rows["run"] and body["risk_run"]["model"] == risk_model.MODEL_VERSION
    assert body["risk_run"]["fields_ranked"] == _ranked_fields()
    # a rank, never a probability or a score
    flat = json.dumps(body["watchlist"])
    assert "probability" not in flat and "score" not in flat
    reading = body["ranking_reliability"]["reading"]
    assert "1.42 times as often as the average field (95% CI 1.24-1.73): 21.6% against 15.2%" in reading
    assert "the ranking is weak (AUC 0.560, 95% CI 0.491-0.663; 0.5 is chance)" in reading
    assert "over all 148 pass pairs" in reading and "cannot be reconstructed" in reading
    assert reading.endswith(brief.RANK_NOT_PROBABILITY)
    assert body["ranking_reliability"]["all_pairs"]["auc_ci95"] == [0.491, 0.663]
    assert "the burn-risk run of 2026-10-31 13:40 IST" in body["notes"][3]
    limited = client.get(f"/api/brief/{A}", params={"limit": 1}).json()
    assert len(limited["villages"]) == len(limited["watchlist"]) == 1 and limited["villages_total"] == 4
    assert limited["totals"] == body["totals"]  # the totals always cover every village
    cc = r.headers["cache-control"]
    assert "s-maxage=" in cc and "stale-while-revalidate=172800" in cc, cc


def test_unknown_district_is_a_404_that_names_the_districts(client: TestClient, rows: Any) -> None:
    for path in ("/api/brief/Atlantis", "/api/brief/Atlantis/csv"):
        r = client.get(path)
        assert r.status_code == 404, path
        body = r.json()
        assert body["status"] == 404 and body["sources"] == []
        assert "no district named 'Atlantis'" in body["detail"] and A in body["detail"] and B in body["detail"]
        assert "s-maxage" not in r.headers.get("cache-control", "")


def test_a_district_with_no_field_in_the_top_tenth_is_never_called_top_tenth(client: TestClient, rows: Any) -> None:
    """Many districts have few villages with fields in the run's top tenth, and some have none (on the
    dev database on 1 Nov 2026, 21 of 36 districts had fewer than ten and Karnal none): the brief
    still lists their villages by best rank, flagged, and the investigator's tool never hands them
    to the model under the top-tenth name (no number check would catch that claim)."""
    body = client.get(f"/api/brief/{B}", params={"days": 7}).json()
    assert body["totals"]["top_tenth_villages"] == 0 and body["watchlist_in_top_tenth"] == 0
    assert [(w["village"], w["fields"], w["top_tenth_fields"], w["in_top_tenth"], w["best_rank"])
            for w in body["watchlist"]] == [("Delta", 14, 0, False, 17)]

    async def go() -> list[tools.ToolResult]:
        async with tools.InternalApi() as api:
            return [await tools.run_tool(api, "get_field_brief", {"district": B}),
                    await tools.run_tool(api, "get_field_brief", {"district": A})]

    none, some = asyncio.run(go())
    assert none.ok and some.ok, (none.error, some.error)
    data = none.data
    assert "watchlist_top_tenth" not in data and "more_watchlist_top_tenth_villages" not in data
    assert data["watchlist_note"] == f"No harvested field in {B} ranks in the risk run's top tenth."
    assert [(w["village"], w["harvested_fields"], w["best_rank"]) for w in data["best_ranked_outside_top_tenth"]] \
        == [("Delta", 14, 17)]
    assert data["villages_outside_top_tenth"] == 1
    assert "top_tenth" not in "".join(json.dumps(w) for w in data["best_ranked_outside_top_tenth"])
    assert "; 0 villages with fields in the risk run's top tenth" in none.summary
    # a district with both: only the villages with top-tenth fields carry the name
    top = [(w["village"], w["fields_in_top_tenth"]) for w in some.data["watchlist_top_tenth"]]
    assert top == [("Alpha", 2), ("Beta", 1)] and "watchlist_note" not in some.data
    outside = [(w["village"], w["best_rank"]) for w in some.data["best_ranked_outside_top_tenth"]]
    assert outside == [("Alpha", 4), ("Zeta", 10), ("unnamed (no census village close enough)", 15)]
    assert some.data["villages_outside_top_tenth"] == 3


def test_reliability_says_when_the_backtest_tested_another_model(rows: Any) -> None:
    with session_scope() as s:
        mismatch = brief._ranking_reliability(s, {"model": "risk-v2"})
    assert mismatch["reading"].startswith(f"The stored backtest tested {risk_model.MODEL_VERSION}; "
                                          "today's ranks come from risk-v2.")


# --- the CSV --------------------------------------------------------------------------------------


def _csv(client: TestClient, lang: str) -> tuple[Any, list[list[str]]]:
    r = client.get(f"/api/brief/{A}/csv", params={"days": 7, "lang": lang})
    assert r.status_code == 200, r.text
    return r, list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))


def test_csv_opens_in_excel_with_links_in_three_languages(client: TestClient, rows: Any) -> None:
    ids = rows["scars"]
    r, table = _csv(client, "en")
    assert r.content.startswith(b"\xef\xbb\xbf")  # the BOM: Excel then reads UTF-8
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    # the file's name says which passes it counts
    assert r.headers["content-disposition"] == \
        'attachment; filename="blindspot-brief-t-b-nagar-2026-10-24-to-2026-10-30.csv"'
    assert "stale-while-revalidate=172800" in r.headers["cache-control"]
    # the JSON brief of the same window lists the sources of every figure
    assert r.headers["link"] == '</api/brief/T.B.%20Nagar?days=7>; rel="describedby"'
    header, *lines = table
    window, audit = brief.Window.ending_at(NEWEST, 7), brief.candidate_unseen_audit()
    assert header == brief.csv_header("en", audit, window) and len(header) == 10
    # a saved file still says the counts and areas are upper bounds, with the audit and the passes
    assert header[3] == ("candidate unseen burns, Sentinel-2 passes 2026-10-24 to 2026-10-30 (upper bound: "
                         "in a hand audit of 23, 87% were real burns, 95% CI 68%-96%)")
    assert header[4] == ("area of candidate unseen burns, ha (upper bound: in a hand audit of 23, 87% were real "
                         "burns, 95% CI 68%-96%)")
    assert lines[0] == ["Alpha", "North", A, "2", "8.00", "2026-10-30", "-8.000000", "65.010000",
                        "https://www.google.com/maps/search/?api=1&query=-8.000000,65.010000",
                        f"https://blindspot-web-five.vercel.app/case/{ids['a2']}"]
    assert [line[0] for line in lines] == ["Alpha", "Beta", brief.UNNAMED["en"], "Alpha"]
    for line in lines:
        lat, lon = line[6], line[7]
        assert line[8] == f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        assert re.fullmatch(r"https://blindspot-web-five\.vercel\.app/case/\d+", line[9])
    for lang in ("hi", "pa"):
        r, (header, *localized) = _csv(client, lang)
        assert r.content.startswith(b"\xef\xbb\xbf")
        assert header == brief.csv_header(lang, audit, window) != brief.csv_header("en", audit, window)
        assert foreign_script(" ".join(header), lang) == []  # Devanagari for Hindi, Gurmukhi for Punjabi
        for cell in header[3:5]:  # the audit in every language, its numbers in the digits 0-9
            assert all(s in cell for s in ("23 ", "87%", "95% CI 68%-96%")), cell
        assert "2026-10-24" in header[3] and "2026-10-30" in header[3]
        assert localized[2][0] == brief.UNNAMED[lang] and foreign_script(brief.UNNAMED[lang], lang) == []
        for line in localized:  # numbers in the digits 0-9
            assert all(re.fullmatch(r"-?[0-9]+(\.[0-9]+)?|[0-9]{4}-[0-9]{2}-[0-9]{2}", c) for c in line[3:8]), line
        assert [line[3:] for line in localized] == [line[3:] for line in lines]
    assert client.get(f"/api/brief/{A}/csv", params={"lang": "xx"}).status_code == 422
    assert brief.csv_filename("S.A.S Nagar", window.first, window.last) == \
        "blindspot-brief-s-a-s-nagar-2026-10-24-to-2026-10-30.csv"


def test_csv_names_never_run_as_formulas() -> None:
    """Village names include OpenStreetMap places, which anyone can edit, and the CSV is made for
    Excel: a name that starts like a formula is written as text (CSV injection). Numbers are not
    names: a negative latitude keeps its minus."""
    starts = ("=1+1", "+91", "-x", "@SUM(A1)", "\t=1", "\r=1", "Alpha", "", "a=1")
    assert [brief.text_cell(s) for s in starts] == ["'=1+1", "'+91", "'-x", "'@SUM(A1)", "'\t=1", "'\r=1",
                                                    "Alpha", "", "a=1"]
    window = brief.Window.ending_at(NEWEST, 7)
    row = {"village": '=HYPERLINK("http://example.invalid","x")', "subdistrict": "@North", "unseen_clusters": 1,
           "unseen_area_ha": 2.5, "newest_pass": NEWEST.date(), "lat": -8.0, "lon": 65.0,
           "maps_url": brief.maps_url(-8.0, 65.0), "case_url": brief.case_url(1)}
    v = brief._Villages({"district": "+Test", "state": "Punjab", "newest": NEWEST}, window, [row], 1, 2.5)
    raw = brief.villages_csv(v, "en", brief.candidate_unseen_audit()).decode("utf-8-sig")
    _, line = list(csv.reader(io.StringIO(raw)))
    assert line[:3] == ['\'=HYPERLINK("http://example.invalid","x")', "'@North", "'+Test"]
    assert line[3:7] == ["1", "2.50", "2026-10-30", "-8.000000"]


# --- the investigator's tool ----------------------------------------------------------------------


def test_field_brief_tool_feeds_the_answer_check(rows: Any) -> None:
    """The tool returns a compact brief whose numbers the verifier finds and whose candidate unseen
    areas the honesty check holds to the audit, like get_burn_scars' (no scores, no probabilities)."""
    args = {"district": "tb nagar", "days": 7}

    async def go() -> list[tools.ToolResult]:
        async with tools.InternalApi() as api:
            return [await tools.run_tool(api, "get_field_brief", args),
                    await tools.run_tool(api, "get_field_brief", {"district": "Atlantis"}),
                    await tools.run_tool(api, "get_field_brief", {})]

    result, unknown, empty = asyncio.run(go())
    assert result.ok, result.error
    data = result.data
    assert data["district"] == A and data["window"]["last_pass_day"] == "2026-10-30"
    assert data["window"]["newest_pass"] == "2026-10-30 11:10 IST"
    assert data["totals"]["unseen_area_ha"] == 12.5 and data["totals"]["top_tenth_villages"] == 2
    villages = data["villages_with_candidate_unseen_burns"]
    assert len(villages) == 4 and "more_villages" not in data
    assert villages[0] == {"village": "Alpha", "subdistrict": "North", "centroid_lon_lat": [65.01, -8.0],
                           "unseen_clusters": 2, "unseen_area_ha": 8.0, "newest_pass": "2026-10-30",
                           "case_ids": [rows["scars"]["a2"], rows["scars"]["a1"]]}
    assert villages[2]["village"].startswith("unnamed")
    watch = data["watchlist_top_tenth"]
    assert (watch[0]["village"], watch[0]["harvested_fields"], watch[0]["fields_in_top_tenth"], watch[0]["best_rank"]) \
        == ("Alpha", 5, 2, 2)
    assert "watchlist_harvested_fields" not in data and "best_ranked_outside_top_tenth" in data
    description = tools.TOOLS["get_field_brief"].description
    assert "watchlist_top_tenth" in description and "never call them top tenth" in description
    assert data["candidate_unseen_audit"]["precision_pct"] == 87.0
    assert data["candidate_unseen_audit"]["precision_ci95_pct"] == [67.9, 95.5]
    assert data["ranking_reliability"]["strength"] == "weak" and len(data["notes"]) == 5
    flat = json.dumps(data)
    assert "risk_score" not in flat and "probability" not in flat
    assert "s2_cog" in {ref.key for ref in result.sources}
    assert "candidate unseen burns" in result.summary and "95% CI 67.9-95.5%" in result.summary
    assert not unknown.ok and "HTTP 404" in (unknown.error or "") and "Atlantis" in (unknown.error or "")
    assert not empty.ok and "district" in (empty.error or "")
    assert "get_field_brief" in tools.TOOLS and "get_field_brief" in [d.name for d in tools.declarations()]
    assert "call get_field_brief" in prompts.INVESTIGATOR_SYSTEM
    # the runner counts tool descriptions as evidence: this one adds no number ("Sentinel-2" is a name)
    assert extract_claims(tools.TOOLS["get_field_brief"].description) == []

    # What the model is shown, as the runner records it.
    book = tools.CitationBook()
    seen = [{"name": "get_field_brief", "args": args, **tools.model_payload(result, book)}]
    marker = next(int(s["marker"].strip("[]")) for s in seen[0]["output"]["sources"]
                  if s["source"] == label_for("s2_cog"))
    facts = honesty_facts([{"args": t["args"], "output": t["output"]} for t in seen])
    assert facts.precision == AUDIT["precision"] and facts.precision_ci == tuple(AUDIT["precision_ci95"])
    assert facts.audited == 23 and {8.0, 12.5} <= set(facts.unseen_area_ha)
    assert facts.auc_all is not None and facts.auc_all["auc"] == 0.56 and facts.auc_all["ci"] == (0.491, 0.663)
    assert facts.auc_all["strength"] == "weak" and facts.risk_scores == [] and facts.top_village is None
    bare = f"Alpha in {A} had 8.0 ha of unseen burns [{marker}]."
    assert [i.kind for i in check_honesty([bare], "en", facts)] == ["unseen_not_candidate"]
    good = (f"Alpha in {A} had 8.0 ha of candidate unseen burns [{marker}]. In an audit of 23 candidates, 87% were "
            f"real burns (95% CI 68%-96%) [{marker}], so that area is an upper bound.")
    assert check_honesty([good], "en", facts) == []
    report = verify(good, build_evidence([{"args": args, "output": seen[0]["output"]}]), set(book.refs),
                    runner.attributed(seen))
    assert report.passed and report.verified == report.checked >= 5, report.unverified
    invented = verify(good.replace("8.0 ha", "9.7 ha"), build_evidence([{"args": args, "output": seen[0]["output"]}]),
                      set(book.refs), runner.attributed(seen))
    assert not invented.passed and invented.unverified[0].startswith("9.7 ha")
