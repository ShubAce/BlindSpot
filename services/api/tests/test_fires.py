"""Fires module tests: FIRMS parsing and storage, CREAMS parsing, fire statistics, /api/fires/*.

Every upstream answer used here is a real response recorded on 2026-10-30 under
tests/fixtures/fires/ (files trimmed to fewer rows, never edited). The database tests use the
blindspot_test database and delete only the rows they created.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.analytics import fires_stats
from blindspot.analytics.fires_stats import observed_windows, season_start
from blindspot.db import SessionLocal, init_db, session_scope
from blindspot.http import FetchResult
from blindspot.jobs.firms import (
    TransactionBudget,
    choose_window,
    label_admin,
    upsert_detections,
    windows_for,
)
from blindspot.provenance import record_fetch, utcnow
from blindspot.sources import creams
from blindspot.sources.celestrak import SATELLITE_BY_NAME
from blindspot.sources.firms import (
    FIRE_SATELLITES,
    PRODUCT_BY_SOURCE,
    FirmsFormatError,
    KeyStatus,
    clip_to_bbox,
    parse_csv,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fires"
AOI = (73.8, 27.6, 77.7, 32.6)
N20 = PRODUCT_BY_SOURCE["VIIRS_NOAA20_NRT"]
MODIS = PRODUCT_BY_SOURCE["MODIS_NRT"]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module", autouse=True)
def database() -> None:
    init_db()


# --- parsing -------------------------------------------------------------------------------------


def test_keyless_viirs_file_parses_to_utc_rows() -> None:
    parsed = parse_csv(fixture_text("J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"), N20)
    assert len(parsed.rows) == 626 and not parsed.skipped
    first = parsed.rows[0]
    assert first.satellite == "NOAA-20" and first.instrument == "VIIRS"
    assert first.acq_utc.tzinfo is UTC and first.confidence in ("low", "nominal", "high")
    # acq_time arrives zero-padded ("0809") and is UTC: the 10-30 daytime pass at 08:09 UTC.
    assert datetime(2026, 10, 30, 8, 9, tzinfo=UTC) in {r.acq_utc for r in parsed.rows}
    newest = max(r.acq_utc for r in parsed.rows)  # the heartbeat the job records per satellite
    assert parsed.newest_by_satellite == {"NOAA-20": newest}
    # The South Asia file reaches Iran to Thailand; the box keeps only our area's rows.
    assert len(clip_to_bbox(parsed.rows, AOI)) == 611


def test_modis_rows_map_brightness_and_confidence_classes() -> None:
    parsed = parse_csv(fixture_text("MODIS_C6_1_South_Asia_24h_20261030T1739Z.csv"), MODIS)
    assert {r.satellite for r in parsed.rows} <= {"Aqua", "Terra"}
    assert all(r.bright_ti4 and r.bright_ti5 for r in parsed.rows)  # brightness, bright_t31
    for r in parsed.rows:  # MODIS C6.1 user guide classes: low < 30 <= nominal < 80 <= high
        expected = "low" if r.confidence_pct < 30 else "nominal" if r.confidence_pct < 80 else "high"
        assert r.confidence == expected


def test_area_api_and_keyless_file_give_identical_rows() -> None:
    # The unique key only dedupes the two feeds if both normalise a detection identically.
    keyless = parse_csv(fixture_text("J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"), N20).rows
    area = parse_csv(fixture_text("area_VIIRS_NOAA20_NRT_2d_20261029.csv"), N20).rows
    assert len(area) == 611
    assert set(area) == set(clip_to_bbox(keyless, AOI))  # same 611 rows, field for field
    # the area API writes acq_time unpadded ("828") and confidence as l/n/h
    assert "828" in fixture_text("area_VIIRS_NOAA20_NRT_2d_20261029.csv")


def test_area_api_modis_names_and_unpadded_times() -> None:
    parsed = parse_csv(fixture_text("area_MODIS_NRT_5d_20261026.csv"), MODIS)
    assert len(parsed.rows) == 383 and not parsed.skipped
    assert set(Counter(r.satellite for r in parsed.rows)) == {"Aqua", "Terra"}
    assert datetime(2026, 10, 26, 4, 10, tzinfo=UTC) in {r.acq_utc for r in parsed.rows}  # "410"


def test_firms_error_text_is_not_mistaken_for_data() -> None:
    body = fixture_text("area_error_day_range.txt")
    assert body.startswith("Invalid day range")
    with pytest.raises(FirmsFormatError):
        parse_csv(body, N20)


def test_satellite_names_are_shared_with_the_orbit_catalogue() -> None:
    assert set(FIRE_SATELLITES) <= set(SATELLITE_BY_NAME)
    assert set(creams.SATELLITE_NAMES.values()) <= set(SATELLITE_BY_NAME)
    assert set(fires_stats.FIRE_SATELLITES) == set(FIRE_SATELLITES)


# --- job helpers ---------------------------------------------------------------------------------


def test_choose_window_overlaps_the_last_sync() -> None:
    now = datetime(2026, 10, 30, 18, tzinfo=UTC)
    assert choose_window(None, now) == "7d"
    assert choose_window(now - timedelta(minutes=30), now) == "24h"
    assert choose_window(now - timedelta(hours=20), now) == "48h"
    assert choose_window(now - timedelta(days=3), now) == "7d"


def test_backfill_windows_respect_the_five_day_limit() -> None:
    days = [date(2026, 9, 15) + timedelta(days=i) for i in range(12)] + [date(2026, 10, 1)]
    assert windows_for(days) == [
        (date(2026, 9, 15), 5), (date(2026, 9, 20), 5), (date(2026, 9, 25), 2),
        (date(2026, 10, 1), 1),
    ]  # fmt: skip


def test_transaction_budget_counts_other_users_of_the_key() -> None:
    budget = TransactionBudget(KeyStatus(limit=5000, used=25, interval="10 minutes"))
    assert budget.spend(10) == 0.0  # fits at once
    busy = TransactionBudget(KeyStatus(limit=5000, used=2600, interval="10 minutes"))
    with pytest.raises(RuntimeError, match="busy"):
        busy.spend(10)  # others already use more than our half; nothing of ours to wait for


def test_season_starts_on_15_september() -> None:
    assert season_start(date(2026, 10, 30)) == date(2026, 9, 15)
    assert season_start(date(2026, 9, 15)) == date(2026, 9, 15)
    assert season_start(date(2027, 3, 1)) == date(2026, 9, 15)


# --- storage -------------------------------------------------------------------------------------


def test_upsert_is_idempotent_and_labels_once() -> None:
    rows = clip_to_bbox(
        parse_csv(fixture_text("J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"), N20).rows, AOI
    )
    fetch_id = record_fetch("firms", "tests/fixtures/fires/J1 (test)", ok=True,
                            started_at=utcnow(), meta={"test": "test_fires"})  # fmt: skip
    keys = {"src": [r.source for r in rows], "t": [r.acq_utc for r in rows],
            "lat": [r.lat for r in rows], "lon": [r.lon for r in rows]}  # fmt: skip
    count_sql = text(
        "SELECT count(*) FROM fire_detection f JOIN unnest(CAST(:src AS text[]), "
        "CAST(:t AS timestamptz[]), CAST(:lat AS float8[]), CAST(:lon AS float8[])) "
        "AS k(src, t, lat, lon) ON f.source = k.src AND f.acq_utc = k.t AND f.lat = k.lat "
        "AND f.lon = k.lon"
    )
    try:
        with session_scope() as session:
            upsert_detections(session, rows, fetch_id)
        with session_scope() as session:
            assert session.execute(count_sql, keys).scalar() == len(rows) == 611
            assert upsert_detections(session, rows, fetch_id) == 0  # nothing new the second time
            label_admin(session, "fire_detection")
            version = session.execute(
                text("SELECT count(*) FROM fire_detection WHERE fetch_id = :f "
                     "AND admin_version IS NULL"), {"f": fetch_id}  # fmt: skip
            ).scalar()
            loaded = fires_stats.boundaries_loaded(session)
            assert version == (0 if loaded else session.execute(
                text("SELECT count(*) FROM fire_detection WHERE fetch_id = :f"), {"f": fetch_id}
            ).scalar())  # fmt: skip
    finally:
        with session_scope() as session:
            session.execute(text("DELETE FROM fire_detection WHERE fetch_id = :f"), {"f": fetch_id})


def test_punjab_counts_equal_a_manual_polygon_join() -> None:
    """SYNTHESIS acceptance check: the Punjab count must equal a manual polygon join, never a box.

    Runs in one transaction that is rolled back: the real geoBoundaries IN-PB polygon and the
    recorded detections are visible only to this session, so parallel tests never see them.
    """
    shape = json.loads(fixture_text("geoboundaries_IND_ADM1_IN-PB_simplified.geojson"))
    rows = clip_to_bbox(
        parse_csv(fixture_text("J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"), N20).rows, AOI
    ) + clip_to_bbox(
        parse_csv(fixture_text("MODIS_C6_1_South_Asia_24h_20261030T1739Z.csv"), MODIS).rows, AOI
    )
    end = datetime(2026, 10, 30, 18, tzinfo=UTC)
    with SessionLocal() as session:
        try:
            session.execute(text("DELETE FROM boundary"))  # only this transaction's view
            session.execute(
                text(
                    "INSERT INTO boundary (level, name, state, code, geom, source) VALUES "
                    "('state', 'Punjab', 'Punjab', '3', "
                    "ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326)), 'geoBoundaries (test)')"
                ),
                {"g": json.dumps(shape["features"][0]["geometry"])},
            )
            upsert_detections(session, rows, None)
            label_admin(session, "fire_detection")
            manual = session.execute(
                text(
                    "SELECT count(*) FROM fire_detection f JOIN boundary b ON b.level = 'state' "
                    "AND b.name = 'Punjab' AND ST_Within(f.geom, b.geom) "
                    "WHERE f.acq_utc >= :t0 AND f.acq_utc < :t1"
                ),
                {"t0": end - timedelta(days=2), "t1": end},
            ).scalar()
            profile, sources = fires_stats.hours_profile(session, 2, "punjab", end)
            assert profile["total"] == manual > 0
            assert manual < len(rows)  # the box also holds Pakistan's Punjab: clipped, not counted
            assert sum(sum(v) for v in profile["by_satellite"].values()) == manual
            payload, _ = fires_stats.daily_series(session, date(2026, 10, 29), date(2026, 10, 30),
                                                  "punjab")  # fmt: skip
            assert payload["totals"]["count"] <= manual  # IST days inside the 48 h window
            assert profile["unobserved_span_ist"] is not None
        finally:
            session.rollback()


def test_counts_are_withheld_without_state_polygons() -> None:
    with SessionLocal() as session:
        session.execute(text("DELETE FROM boundary"))  # inside a transaction that is rolled back
        rf = fires_stats.region_filter(session, "punjab")
        assert not rf.available and "Pakistan" in (rf.note or "")
        payload, _ = fires_stats.daily_series(session, date(2026, 10, 1), date(2026, 10, 2), "all")
        assert payload["series"] == [] and payload["note"]
        session.rollback()


# --- observed windows and the blind span ---------------------------------------------------------


def _ist_minutes(rows: list) -> tuple[Counter[int], dict[int, Counter[str]]]:
    minutes: Counter[int] = Counter()
    flags: dict[int, Counter[str]] = {}
    for r in rows:
        local = r.acq_utc + timedelta(hours=5, minutes=30)
        m = local.hour * 60 + local.minute
        minutes[m] += 1
        flags.setdefault(m, Counter())[r.daynight] += 1
    return minutes, flags


def test_blind_span_starts_after_the_last_daytime_window() -> None:
    # Real detections in our area: NOAA-20 (day and night passes) and MODIS (Aqua day, Terra night).
    rows = clip_to_bbox(
        parse_csv(fixture_text("J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"), N20).rows, AOI
    ) + clip_to_bbox(
        parse_csv(fixture_text("MODIS_C6_1_South_Asia_24h_20261030T1739Z.csv"), MODIS).rows, AOI
    )
    minutes, flags = _ist_minutes(rows)
    windows, gaps, span = observed_windows(minutes, None, flags)
    assert sum(w["count"] for w in windows) == len(rows)
    day_windows = [w for w in windows if w["daynight"] == "D" and not w["minor"]]
    assert span is not None and span["start"] == max(w["end"] for w in day_windows)
    assert span["rule"].startswith("from the end of the last daytime window")
    assert span in [{**g, "rule": span["rule"]} for g in gaps]


def test_windows_wrap_midnight_and_minor_windows_do_not_split_gaps() -> None:
    minutes = Counter({23 * 60 + 50: 5, 10: 5, 13 * 60: 100, 20 * 60: 1})
    flags = {23 * 60 + 50: Counter(N=5), 10: Counter(N=5), 13 * 60: Counter(D=100),
             20 * 60: Counter(N=1)}  # fmt: skip
    windows, gaps, span = observed_windows(minutes, None, flags)
    assert [(w["start"], w["end"], w["minor"]) for w in windows] == [
        ("13:00", "13:00", False), ("20:00", "20:00", True), ("23:50", "00:10", False)
    ]  # fmt: skip
    # The longest gap is 00:10 -> 13:00 (12.8 h), but the blind span is the one after daylight.
    assert max(g["hours"] for g in gaps) == pytest.approx(12.83, abs=0.01)
    assert span is not None and (span["start"], span["end"]) == ("13:00", "23:50")
    assert span["detections"] == 1 and span["hours"] == pytest.approx(10.83, abs=0.01)


# --- CREAMS --------------------------------------------------------------------------------------


def test_creams_layer_names_and_times() -> None:
    assert creams.layer_name(date(2026, 10, 1)) == "geokrishi_v:1_october_2026_rice"
    body = json.loads(fixture_text("creams_29_october_2026_rice.json"))
    events, skipped = creams.parse_layer(body, date(2026, 10, 29))
    assert len(events) == 40 and skipped == 0
    first = events[0]  # AMRITSAR, S-NPP, acq_time 13:34:58 IST
    assert first.satellite == "Suomi NPP" and first.creams_district == "AMRITSAR"
    assert first.acq_utc == datetime(2026, 10, 29, 8, 4, 58, tzinfo=UTC)  # IST - 5:30
    assert {e.satellite for e in events} <= {"Suomi NPP", "Aqua"}
    # a feature whose date disagrees with the layer is skipped, never moved to another day
    assert creams.parse_layer(body, date(2026, 10, 30)) == ([], 40)


def test_creams_capabilities_list_which_layers_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = fixture_text("creams_capabilities_trimmed.xml")

    def recorded(*_args: object, **_kwargs: object) -> FetchResult:
        return FetchResult(data=doc, fetch_id=None, status_code=200, latency_ms=0,
                           url_logged=creams.CAPABILITIES_URL, source_key="creams")  # fmt: skip

    monkeypatch.setattr(creams, "fetch", recorded)
    days, _ = creams.available_layers()
    # CREAMS published no rice layer for 17 Sep 2026 (only the all-India one)
    assert days == {date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 18)}


# --- endpoints -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stored_fires() -> object:
    """The recorded NOAA-20 and MODIS rows, stored under one test fetch and removed afterwards."""
    rows = clip_to_bbox(
        parse_csv(fixture_text("J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"), N20).rows, AOI
    ) + clip_to_bbox(
        parse_csv(fixture_text("MODIS_C6_1_South_Asia_24h_20261030T1739Z.csv"), MODIS).rows, AOI
    )
    fetch_id = record_fetch("firms", "tests/fixtures/fires (test)", ok=True, started_at=utcnow(),
                            meta={"test": "test_fires endpoints"})  # fmt: skip
    with session_scope() as session:
        upsert_detections(session, rows, fetch_id)
        label_admin(session, "fire_detection")
    yield rows
    with session_scope() as session:
        session.execute(text("DELETE FROM fire_detection WHERE fetch_id = :f"), {"f": fetch_id})


def test_fire_endpoints_follow_the_contract(stored_fires: list) -> None:
    from blindspot.api.main import create_app

    end = "2026-10-30T18:00:00Z"
    with TestClient(create_app()) as client:
        raw = client.get("/api/fires", params={"hours": 48, "end": end, "clip": "none"}).json()
        assert raw["count"] >= len(stored_fires) and raw["clip"] == "none"
        feature = raw["features"]["features"][0]
        assert feature["geometry"]["type"] == "Point" and len(feature["geometry"]["coordinates"]) == 2
        assert {"id", "satellite", "instrument", "acq_utc", "confidence", "frp_mw", "daynight",
                "state", "district"} <= set(feature["properties"])  # fmt: skip
        assert feature["properties"]["acq_utc"].endswith("Z")
        assert set(fires_stats.FIRE_SATELLITES) <= set(raw["by_satellite"])

        clipped = client.get("/api/fires", params={"hours": 48, "end": end}).json()
        daily = client.get("/api/fires/daily", params={"since": "2026-10-28", "until": "2026-10-30"}).json()
        hours = client.get("/api/fires/hours", params={"days": 2, "end": end}).json()
        official = client.get("/api/fires/official", params={"since": "2026-10-28"}).json()
        for body in (raw, clipped, daily, hours, official):
            assert body["generated_at"].endswith("Z") and isinstance(body["sources"], list)
        assert any(s["key"] == "firms" and s["fetch_id"] for s in raw["sources"])
        assert len(hours["bins"]) == 24 and [b["hour_ist"] for b in hours["bins"]] == list(range(24))
        assert {"observed_windows_ist", "unobserved_span_ist", "by_satellite", "total"} <= set(hours)
        assert all(len(v) == 24 for v in hours["by_satellite"].values())
        with SessionLocal() as session:
            loaded = fires_stats.boundaries_loaded(session)
        if loaded:  # clipped to Indian polygons: never more than the unclipped box
            assert 0 < clipped["count"] <= raw["count"]
            assert all(f["properties"]["state"] for f in clipped["features"]["features"])
            assert [s["date"] for s in daily["series"]] == ["2026-10-28", "2026-10-29", "2026-10-30"]
            assert hours["total"] > 0 and hours["observed_windows_ist"]
        else:  # honest degradation: no counts over a box that includes Pakistan
            assert clipped["count"] == 0 and clipped["note"]
            assert daily["series"] == [] and daily["note"]

        bad = client.get("/api/fires", params={"min_confidence": "maybe"})
        assert bad.status_code == 422
        backwards = client.get("/api/fires/daily", params={"since": "2026-10-30", "until": "2026-10-01"})
        assert backwards.status_code == 400
