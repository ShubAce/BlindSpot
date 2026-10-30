"""Air-quality module tests.

Parsers are tested on real responses recorded on 2026-10-30 (tests/fixtures/air/). Database and
endpoint tests write those same real rows into the test database through the job's own upsert,
call the routes with `at=` set to the recording time, and delete exactly the rows they wrote (all
tagged through source_fetch.meta.test). A few tiny live calls check today's response shapes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.config import get_settings
from blindspot.db import init_db, session_scope
from blindspot.http import FetchError, fetch
from blindspot.jobs.air import _obs, _reading_row, upsert_obs
from blindspot.models.air import (
    CAMS,
    DATAGOV,
    OPENAQ,
    SUBINDEX_UNIT,
    UGM3,
    CpcbIndex,
    StationRef,
    canonical_city,
    site_key,
    split_station_name,
)
from blindspot.provenance import record_fetch, utcnow
from blindspot.sources import cams, datagov, iitm_dss, openaq

FIXTURES = Path(__file__).parent / "fixtures" / "air"
TAG = "test_air"
AT = datetime(2026, 10, 30, 17, 0, tzinfo=UTC)  # just after the recordings (22:30 IST)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def locations() -> dict[int, openaq.Location]:
    return {loc.id: loc for loc in openaq.parse_locations(load("openaq_locations.json"))}


# --- station names ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "site", "city", "agency"),
    [
        ("Golden Temple, Amritsar - PPCB", "Golden Temple, Amritsar", "Amritsar", "PPCB"),
        ("Sector - 125, Noida, UP - UPPCB", "Sector - 125, Noida", "Noida", "UPPCB"),
        (
            "Knowledge Park - III, Greater Noida - UPPCB",
            "Knowledge Park - III, Greater Noida",
            "Greater Noida",
            "UPPCB",
        ),
        (
            "IHBAS, Dilshad Garden,New Delhi - CPCB",
            "IHBAS, Dilshad Garden, New Delhi",
            "Delhi",
            "CPCB",
        ),
        (
            "General Hospital, Mandikhera(Nuh) - HSPCB",
            "General Hospital, Mandikhera(Nuh)",
            "Mandikhera",
            "HSPCB",
        ),
        (
            "Shiv Nadar University, Greater Noida-UPPCB",
            "Shiv Nadar University, Greater Noida",
            "Greater Noida",
            "UPPCB",
        ),
        (
            "Municipal Corporation Office, Dharuhera -  HSPCB",
            "Municipal Corporation Office, Dharuhera",
            "Dharuhera",
            "HSPCB",
        ),
        ("IGI Airport", "IGI Airport", None, None),
    ],
)
def test_split_station_name(name: str, site: str, city: str | None, agency: str | None) -> None:
    assert split_station_name(name) == (site, city, agency)


def test_site_key_matches_the_two_feeds_spellings() -> None:
    assert site_key("Anand Vihar, New Delhi - DPCC") == site_key("Anand Vihar, Delhi - DPCC")
    assert site_key("Sector - 125, Noida, UP - UPPCB") == site_key("Sector - 125, Noida - UPPCB")
    assert site_key("General Hospital, Mandikhera - HSPCB") == site_key(
        "General Hospital, Mandikhera(Nuh) - HSPCB"
    )
    assert canonical_city("New Delhi") == "Delhi"
    assert canonical_city("  Ludhiana ") == "Ludhiana"


def test_cpcb_index_labels_openaq_stations_from_cpcbs_feed(
    locations: dict[int, openaq.Location],
) -> None:
    rows = datagov.parse_records(load("datagov_page.json")["records"])
    index = CpcbIndex(
        {r.station: StationRef(r.station, r.city, r.state, r.lat, r.lon) for r in rows}.values()
    )
    anand = locations[235]  # OpenAQ spells it "Anand Vihar, New Delhi - DPCC"
    assert index.match(anand.name, anand.lat, anand.lon).name == "Anand Vihar, Delhi - DPCC"
    assert index.label(anand.name, anand.lat, anand.lon) == ("Delhi", "Delhi")
    pusa = locations[6356]  # two CPCB stations share the site "Pusa": same agency wins
    assert index.match(pusa.name, pusa.lat, pusa.lon).name == "Pusa, Delhi - DPCC"
    # One to one: the IMD-run Pusa station (filed as IITM by data.gov.in) gets the other entry.
    imd = locations[5404]
    twins = index.match_all(
        [("6356", pusa.name, pusa.lat, pusa.lon), ("5404", imd.name, imd.lat, imd.lon)]
    )
    assert twins["6356"].name == "Pusa, Delhi - DPCC" and twins["5404"].name == "Pusa, Delhi - IITM"
    ludhiana = locations[5569]
    assert index.label(ludhiana.name, ludhiana.lat, ludhiana.lon) == ("Ludhiana", "Punjab")
    rupnagar = locations[6442]  # not in CPCB's feed: city from its name, no state invented
    assert index.match(rupnagar.name, rupnagar.lat, rupnagar.lon) is None
    assert index.label(rupnagar.name, rupnagar.lat, rupnagar.lon) == ("Rupnagar", None)


# --- OpenAQ parsing -----------------------------------------------------------------------------


def test_parse_locations_keeps_pm_sensors_only(locations: dict[int, openaq.Location]) -> None:
    assert len(locations) == 9
    rk_puram = locations[17]
    assert rk_puram.name == "R K Puram, Delhi - DPCC"
    assert sorted(rk_puram.sensors.values()) == ["pm10", "pm10", "pm25", "pm25"]  # legacy + live
    assert rk_puram.last_utc == datetime(2026, 10, 30, 16, 30, tzinfo=UTC)
    assert locations[13].last_utc.year == 2018  # retired station: the job skips it


def test_parse_latest_takes_the_live_sensor_not_the_legacy_one(
    locations: dict[int, openaq.Location],
) -> None:
    readings = openaq.parse_latest(load("openaq_latest_17.json"), locations[17])
    assert {r.parameter for r in readings} == {"pm25", "pm10"}
    pm25 = next(r for r in readings if r.parameter == "pm25")
    assert pm25.sensor_id == 12234787  # sensor 35 last reported in 2018
    assert pm25.observed_at.year == 2026 and pm25.period_minutes == 15
    assert pm25.value >= 0


def test_parse_hourly_reads_ist_aligned_hours(locations: dict[int, openaq.Location]) -> None:
    payload = load("openaq_hourly_50.json")
    readings = openaq.parse_hourly(payload, location_id=50, sensor_id=12234796, parameter="pm25")
    assert readings and all(r.period_minutes == 60 for r in readings)
    # CPCB hours are IST clock hours, which end at :30 UTC.
    assert all(r.observed_at.minute == 30 for r in readings)
    dropped = sum(
        1
        for x in payload["results"]
        if (x.get("flagInfo") or {}).get("hasFlags") or x["coverage"]["percentComplete"] < 75
    )
    assert dropped > 0  # the recorded week has hours built from 1 or 2 of 4 readings
    assert len(readings) == len(payload["results"]) - dropped
    # 2026-10-30 05:30Z rests on a single 15-minute reading: never served as an hourly mean
    assert all(r.observed_at.isoformat() != "2026-10-30T05:30:00+00:00" for r in readings)


# --- data.gov.in parsing and NAQI ----------------------------------------------------------------


def test_datagov_rows_are_subindices_at_the_ist_batch_time() -> None:
    page = load("datagov_page.json")
    rows = datagov.parse_records(page["records"])
    na = sum(1 for r in page["records"] if r["avg_value"] == "NA")
    assert na > 0 and len(rows) == len(page["records"]) - na  # "NA" dropped, never guessed
    assert {r.observed_at for r in rows} == {
        datetime(2026, 10, 30, 16, 30, tzinfo=UTC)
    }  # 22:00 IST
    amritsar = {r.pollutant: r for r in rows if r.station == "Golden Temple, Amritsar - PPCB"}
    assert amritsar["pm25"].window_minutes == 1440 and amritsar["co"].window_minutes == 480
    inside = datagov.parse_records(page["records"], bbox=(75.0, 30.0, 77.0, 31.0))
    assert inside and all(75.0 <= r.lon <= 77.0 and 30.0 <= r.lat <= 31.0 for r in inside)


def test_station_aqi_follows_cpcbs_rule() -> None:
    assert datagov.station_aqi({"pm25": 167, "pm10": 120, "no2": 30}) == (167, ["pm25"])
    assert datagov.station_aqi({"pm25": 167, "no2": 30}) == (None, [])  # fewer than 3 pollutants
    assert datagov.station_aqi({"no2": 30, "so2": 10, "co": 50}) == (None, [])  # no PM
    assert datagov.aqi_category(50) == "Good" and datagov.aqi_category(51) == "Satisfactory"
    assert datagov.aqi_category(178) == "Moderate" and datagov.aqi_category(401) == "Severe"
    # 167 lies in the Moderate band: 61-90 µg/m³ <-> 101-200.
    assert datagov.subindex_to_concentration("pm25", 167) == pytest.approx(80.3, abs=0.05)
    assert datagov.subindex_to_concentration("pm25", 450) is None  # open-ended Severe band
    assert datagov.subindex_to_concentration("no2", 50) is None


# --- CAMS and DSS parsing -----------------------------------------------------------------------


def test_cams_parses_meta_and_a_multi_location_answer() -> None:
    run = cams.parse_meta(load("cams_meta.json"))
    assert run.available > run.initialised
    points = [cams.Point("Delhi", 28.635, 77.19), cams.Point("Ludhiana", 30.9028, 75.8086)]
    series = cams.parse_series(load("cams_two_points.json"), points)
    assert [s.name for s in series] == ["Delhi", "Ludhiana"]
    assert (series[0].lat, series[0].lon) == pytest.approx((28.6, 77.2), abs=0.01)  # model cell
    assert len(series[0].times) == 288 == len(series[0].values["pm25"])  # 7 days back + 5 ahead
    with pytest.raises(ValueError):
        cams.parse_series(load("cams_two_points.json"), points[:1])


def test_dss_parser_reads_both_charts() -> None:
    data = iitm_dss.parse_dss((FIXTURES / "dss_charts.html").read_text(encoding="utf-8"))
    assert data.run_date.isoformat() == "2026-10-30"
    assert len(data.daily_pct) == 27 and data.daily_pct["2026-10-30"] == pytest.approx(1.74913)
    assert list(data.forecast_pct) == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
    for shares in data.forecast_pct.values():
        assert sum(shares.values()) == pytest.approx(100.0, abs=0.1)  # every day is a full split
    assert data.forecast_pct["2026-11-01"][iitm_dss.STUBBLE] == pytest.approx(33.4862)
    assert "VIIRS" in data.basis and "climatological" in data.basis
    assert (
        data.sha256()
        == iitm_dss.parse_dss((FIXTURES / "dss_charts.html").read_text(encoding="utf-8")).sha256()
    )
    with pytest.raises(ValueError):
        iitm_dss.parse_dss("<html>maintenance</html>")


# --- database and endpoints ---------------------------------------------------------------------


def _cleanup() -> None:
    with session_scope() as session:
        ids = (
            session.execute(
                text("SELECT id FROM source_fetch WHERE meta->>'test' = :tag"), {"tag": TAG}
            )
            .scalars()
            .all()
        )
        if ids:
            session.execute(text("DELETE FROM air_obs WHERE fetch_id = ANY(:ids)"), {"ids": ids})
            session.execute(
                text("DELETE FROM dss_snapshot WHERE fetch_id = ANY(:ids)"), {"ids": ids}
            )
            session.execute(text("DELETE FROM source_fetch WHERE id = ANY(:ids)"), {"ids": ids})


def _test_fetch(source: str, url: str) -> int:
    fetch_id = record_fetch(
        source,
        url,
        ok=True,
        started_at=AT - timedelta(minutes=40),
        finished_at=AT - timedelta(minutes=39),
        meta={"test": TAG},
    )
    assert fetch_id is not None
    return fetch_id


@pytest.fixture(scope="module")
def stored(locations: dict[int, openaq.Location]) -> Iterator[dict[str, Any]]:
    """The recorded responses written to the test database exactly as the air job writes them."""
    init_db()
    _cleanup()
    index_rows = datagov.parse_records(load("datagov_page.json")["records"])
    index = CpcbIndex(
        {
            r.station: StationRef(r.station, r.city, r.state, r.lat, r.lon) for r in index_rows
        }.values()
    )
    rows: list[dict[str, Any]] = []
    fid = _test_fetch(OPENAQ, "https://api.openaq.org/v3/locations/{id}/latest")
    for loc_id in (17, 50, 235, 5569):
        loc = locations[loc_id]
        label = index.label(loc.name, loc.lat, loc.lon)
        for r in openaq.parse_latest(load(f"openaq_latest_{loc_id}.json"), loc):
            rows.append(_reading_row(r, loc, label, fid))
    hourly_fid = _test_fetch(
        OPENAQ, "https://api.openaq.org/v3/sensors/12234796/measurements/hourly"
    )
    hourly = openaq.parse_hourly(
        load("openaq_hourly_50.json"), location_id=50, sensor_id=12234796, parameter="pm25"
    )
    loc50 = locations[50]
    rows += [
        _reading_row(r, loc50, index.label(loc50.name, loc50.lat, loc50.lon), hourly_fid)
        for r in hourly
    ]
    dg_fid = _test_fetch(DATAGOV, datagov.RESOURCE_URL)
    rows += [
        _obs(
            provider=DATAGOV,
            station_id=r.station,
            station_name=r.station,
            city=r.city,
            state=r.state,
            lat=r.lat,
            lon=r.lon,
            parameter=f"{r.pollutant}_subindex",
            value=float(r.value),
            unit=SUBINDEX_UNIT,
            observed_at=r.observed_at,
            period_minutes=r.window_minutes,
            is_model=False,
            fetch_id=dg_fid,
        )
        for r in index_rows
    ]
    cams_fid = _test_fetch(CAMS, cams.AQ_URL)
    points = [cams.Point("Delhi", 28.635, 77.19), cams.Point("Ludhiana", 30.9028, 75.8086)]
    for s in cams.parse_series(load("cams_two_points.json"), points):
        for t, v in zip(s.times, s.values["pm25"], strict=True):
            rows.append(
                _obs(
                    provider=CAMS,
                    station_id=s.name,
                    station_name=f"CAMS model, {s.name}",
                    city=s.name,
                    state=None,
                    lat=s.lat,
                    lon=s.lon,
                    parameter="pm25",
                    value=v,
                    unit=UGM3,
                    observed_at=t,
                    period_minutes=60,
                    is_model=True,
                    fetch_id=cams_fid,
                )
            )
    with session_scope() as session:
        sent = upsert_obs(session, rows)
    yield {
        "rows": rows,
        "sent": sent,
        "hourly": hourly,
        "fetch_ids": [fid, hourly_fid, dg_fid, cams_fid],
    }
    _cleanup()


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from blindspot.api.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_upsert_is_idempotent(stored: dict[str, Any]) -> None:
    with session_scope() as session:
        before = session.execute(
            text("SELECT count(*) FROM air_obs WHERE fetch_id = ANY(:ids)"),
            {"ids": stored["fetch_ids"]},
        ).scalar()
        upsert_obs(session, stored["rows"])  # the same rows again
        after = session.execute(
            text("SELECT count(*) FROM air_obs WHERE fetch_id = ANY(:ids)"),
            {"ids": stored["fetch_ids"]},
        ).scalar()
    assert before == after == stored["sent"]


def test_latest_is_contract_shaped_and_keeps_model_apart(
    client: TestClient, stored: dict[str, Any]
) -> None:
    body = client.get("/api/air/latest", params={"at": AT.isoformat()}).json()
    assert body["generated_at"].endswith("Z") and body["sources"]
    assert {s["key"] for s in body["sources"]} >= {OPENAQ, DATAGOV, CAMS}
    contract = {
        "id",
        "name",
        "city",
        "state",
        "lat",
        "lon",
        "pm25",
        "pm10",
        "observed_at",
        "provider",
        "is_model",
    }
    stations = body["stations"]
    assert all(contract <= set(s) for s in stations)
    measured = [s for s in stations if s["provider"] == OPENAQ]
    assert {s["id"] for s in measured} == {"openaq:17", "openaq:50", "openaq:235", "openaq:5569"}
    anand = next(s for s in measured if s["id"] == "openaq:235")
    assert anand["city"] == "Delhi" and anand["aqi"]["station"] == "Anand Vihar, Delhi - DPCC"
    assert all(s["is_model"] is False for s in measured)
    model = [s for s in stations if s["is_model"]]
    assert {s["provider"] for s in model} == {CAMS} and len(model) == 2
    # CPCB-feed stations OpenAQ does not carry are listed with their AQI only, never as µg/m³.
    cpcb_only = [s for s in stations if s["provider"] == DATAGOV]
    assert cpcb_only and all(s["pm25"] is None and s["aqi"] for s in cpcb_only)


def test_receptor_is_the_median_of_fresh_stations(
    client: TestClient, stored: dict[str, Any]
) -> None:
    body = client.get("/api/air/latest", params={"at": AT.isoformat()}).json()
    delhi = next(r for r in body["receptors"] if r["city"] == "Delhi")
    contract = {"city", "pm25_now", "pm25_24h_mean", "observed_at", "provider", "n_stations"}
    assert contract <= set(delhi)
    fresh = [
        s["pm25"]
        for s in body["stations"]
        if s["provider"] == OPENAQ
        and s["city"] == "Delhi"
        and not s["stale"]
        and s["pm25"] is not None
    ]
    fresh.sort()
    mid = len(fresh) // 2
    median = fresh[mid] if len(fresh) % 2 else (fresh[mid - 1] + fresh[mid]) / 2
    assert delhi["n_stations"] == len(fresh) == 3
    assert delhi["pm25_now"] == pytest.approx(median, abs=0.05)
    assert delhi["provider"] == OPENAQ
    # The model is reported next to the measurement, never folded into it.
    assert delhi["model"]["provider"] == CAMS and delhi["model"]["pm25"] is not None
    assert delhi["aqi"]["provider"] == DATAGOV


def test_stale_stations_are_labelled_not_hidden(client: TestClient, stored: dict[str, Any]) -> None:
    later = AT + timedelta(hours=5)
    body = client.get("/api/air/latest", params={"at": later.isoformat()}).json()
    measured = [s for s in body["stations"] if s["provider"] == OPENAQ]
    assert len(measured) == 4 and all(s["stale"] and s["age_minutes"] > 120 for s in measured)
    delhi = next(r for r in body["receptors"] if r["city"] == "Delhi")
    assert delhi["pm25_now"] is None and delhi["n_stations"] == 0 and delhi["n_stale"] == 3
    # The newest real value is still offered, aged and flagged, never as pm25_now.
    last = delhi["pm25_last"]
    assert last["stale"] and last["age_minutes"] > 120 and last["n_stations"] >= 1
    assert last["value"] is not None


def test_series_bins_ist_hours_and_keeps_gaps(client: TestClient, stored: dict[str, Any]) -> None:
    body = client.get(
        "/api/air/series", params={"city": "delhi", "hours": 24, "at": AT.isoformat()}
    ).json()
    assert body["city"] == "Delhi" and body["generated_at"] and body["sources"]
    series = body["series"]
    assert len(series) == 24 and all(set(p) == {"t", "pm25", "n_stations"} for p in series)
    assert series[-1]["t"] == "2026-10-30T16:30:00Z"  # 22:00 IST, the last full IST hour
    hourly = {r.observed_at: r.value for r in stored["hourly"]}
    for point in series:
        t = datetime.fromisoformat(point["t"].replace("Z", "+00:00"))
        if point["n_stations"] == 1:  # only Punjabi Bagh has hourly means stored
            assert point["pm25"] == pytest.approx(hourly[t], abs=0.05)
        elif point["n_stations"] == 0:
            assert point["pm25"] is None  # a gap stays a gap
    assert body["model_series"] and all(
        p["t"] <= "2026-10-30T17:00:00Z" for p in body["model_series"]
    )
    ahead = client.get(
        "/api/air/series",
        params={"city": "Delhi", "hours": 6, "ahead_hours": 24, "at": AT.isoformat()},
    ).json()
    assert len(ahead["model_forecast"]) == 24
    assert client.get("/api/air/series", params={"city": "Atlantis"}).status_code == 404


def test_a_dropped_partial_hour_is_not_refilled_by_a_stray_reading(
    client: TestClient, stored: dict[str, Any], locations: dict[int, openaq.Location]
) -> None:
    """The hour ending 05:30Z had 1 of 4 readings, so no hourly mean is stored for it. A
    15-minute reading caught in that hour must not stand in for it either (it may only for hours
    OpenAQ has not averaged yet, after the station's newest hourly mean)."""
    loc = locations[50]
    fid = _test_fetch(OPENAQ, "https://api.openaq.org/v3/locations/50/latest")
    def reading(value: float, hour: int, minute: int) -> openaq.Reading:
        when = datetime(2026, 10, 30, hour, minute, tzinfo=UTC)
        return openaq.Reading(50, 12234796, "pm25", value, when, 15)

    stray, newest = reading(68.0, 5, 15), reading(90.0, 16, 45)
    label = ("Delhi", "Delhi")
    try:
        with session_scope() as session:
            upsert_obs(session, [_reading_row(r, loc, label, fid) for r in (stray, newest)])
        at = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
        series = client.get(
            "/api/air/series", params={"city": "Delhi", "hours": 24, "at": at.isoformat()}
        ).json()["series"]
        by_t = {p["t"]: p for p in series}
        assert by_t["2026-10-30T05:30:00Z"]["n_stations"] == 0  # stays a gap
        assert by_t["2026-10-30T17:30:00Z"]["pm25"] == 90.0  # not averaged yet: the reading counts
    finally:
        _drop_fetches([fid])


def test_stubble_share_endpoint(client: TestClient) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from blindspot.models.air import DssSnapshot

    data = iitm_dss.parse_dss((FIXTURES / "dss_charts.html").read_text(encoding="utf-8"))
    fid = _test_fetch(iitm_dss.SOURCE_KEY, iitm_dss.DSS_URL)
    now = utcnow()
    try:
        with session_scope() as session:
            session.execute(
                pg_insert(DssSnapshot)
                .values(
                    data_sha256=data.sha256(),
                    run_date=data.run_date,
                    daily_pct=data.daily_pct,
                    forecast_pct=data.forecast_pct,
                    basis=data.basis,
                    first_seen_at=now,
                    last_seen_at=now,
                    fetch_id=fid,
                )
                .on_conflict_do_nothing(index_elements=["data_sha256"])  # never touch others' rows
            )
        body = client.get("/api/air/stubble-share").json()
        assert body["run_date"] == "2026-10-30" and body["sources"][0]["key"] == "iitm_dss"
        assert body["today"]["stubble_pct"] == pytest.approx(1.7491)
        assert [d["fire_input"] for d in body["outlook"]] == [
            "viirs_previous_evening",
            "climatology",
            "climatology",
            "climatology",
        ]
        assert len(body["daily"]) == 27 and "VIIRS" in body["method_note"]
    finally:
        _cleanup()


def test_missing_keys_skip_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a key a step says so and fetches nothing; it never substitutes other data."""
    import blindspot.jobs.air as air_jobs
    from blindspot.config import Settings

    no_keys = Settings(openaq_api_key=None, data_gov_in_api_key=None)
    monkeypatch.setattr(air_jobs, "get_settings", lambda: no_keys)
    assert air_jobs.datagov_step() == {"skipped": "DATA_GOV_IN_API_KEY is not set"}
    assert air_jobs.openaq_step() == {"skipped": "OPENAQ_API_KEY is not set"}


# --- robustness (review fixes) ------------------------------------------------------------------


def _drop_fetches(fetch_ids: list[int | None]) -> None:
    """Delete exactly the source_fetch rows a test created, and the rows that cite them."""
    ids = [i for i in fetch_ids if i is not None]
    if not ids:
        return
    with session_scope() as session:
        session.execute(text("DELETE FROM air_obs WHERE fetch_id = ANY(:ids)"), {"ids": ids})
        session.execute(text("DELETE FROM dss_snapshot WHERE fetch_id = ANY(:ids)"), {"ids": ids})
        session.execute(text("DELETE FROM source_fetch WHERE id = ANY(:ids)"), {"ids": ids})


def _fake_result(source: str, url: str, meta: dict[str, Any]) -> Any:
    """A FetchResult for a recorded response, with its own (test-tagged) source_fetch row."""
    from blindspot.http import FetchResult

    now = utcnow()
    fid = record_fetch(source, url, ok=True, started_at=now, meta={**meta, "test": TAG})
    return FetchResult(None, fid, 200, 0, url, source, now)


def test_hourly_reads_start_on_whole_ist_hours() -> None:
    """OpenAQ averages only the readings after datetime_from, so a read that starts mid-hour gets
    that hour back as a partial mean; a week-back read must start on an IST (CPCB) hour."""
    from blindspot.jobs.air import BACKFILL, REREAD, hourly_since

    now = datetime(2026, 10, 30, 17, 56, 53, tzinfo=UTC)
    week = hourly_since(None, now)
    assert week == datetime(2026, 10, 23, 17, 30, tzinfo=UTC)  # 23:00 IST, not 23:26:53
    assert now - BACKFILL - timedelta(hours=1) < week <= now - BACKFILL
    last = datetime(2026, 10, 30, 16, 30, tzinfo=UTC)  # newest stored hour (22:00 IST)
    assert hourly_since(last, now) == last - REREAD
    assert hourly_since(now - timedelta(days=9), now) == week  # never more than a week back


def test_datagov_batch_is_marked_complete_only_after_it_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feed keeps no history: if storing a batch fails, the next run must pull it again
    instead of skipping it as 'unchanged'."""
    import blindspot.jobs.air as air_jobs
    from blindspot.config import Settings

    page = load("datagov_page.json")
    rows = datagov.parse_records(page["records"])
    fid = _fake_result(DATAGOV, datagov.RESOURCE_URL, {"offset": 0}).fetch_id
    batch = datagov.Batch(rows, [fid], [fid] * len(rows), page["updated_date"], page["total"])
    keyed = Settings(data_gov_in_api_key="k" * 40)  # never sent: both calls are replaced below
    monkeypatch.setattr(air_jobs, "get_settings", lambda: keyed)
    monkeypatch.setattr(datagov, "feed_stamp", lambda _key: page["updated_date"])
    monkeypatch.setattr(datagov, "fetch_all", lambda _key, _bbox=None: batch)

    def marked() -> str | None:
        with session_scope() as session:
            return session.execute(
                text("SELECT meta->>'complete_batch' FROM source_fetch WHERE id = :id"),
                {"id": fid},
            ).scalar()

    def failing_write(_session: Any, _rows: Any) -> int:
        raise RuntimeError("database write failed")

    try:
        monkeypatch.setattr(air_jobs, "upsert_obs", failing_write)
        with pytest.raises(RuntimeError, match="database write failed"):
            air_jobs.datagov_step()
        assert marked() is None  # so the next run pulls this batch again
        monkeypatch.setattr(air_jobs, "upsert_obs", upsert_obs)
        assert air_jobs.datagov_step()["rows"] == len(rows)
        assert marked() == page["updated_date"]
        assert air_jobs.datagov_step()["skipped"] == "CPCB feed unchanged since the last pull"
    finally:
        _drop_fetches([fid])


def test_cams_refetch_that_changes_nothing_still_counts_as_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same model run, slightly different sample points, identical values: no row changes, but
    the run must count as stored, or every 30-min run would fetch again until the next run."""
    import blindspot.jobs.air as air_jobs

    run = cams.parse_meta(load("cams_meta.json"))
    fetched: list[int | None] = []

    def fake_series(points: list[cams.Point], **kw: Any) -> tuple[list[cams.ModelSeries], Any]:
        result = _fake_result(CAMS, cams.AQ_URL, {"kind": "air_quality", **kw.get("meta", {})})
        fetched.append(result.fetch_id)
        return cams.parse_series(load("cams_two_points.json"), points), result

    monkeypatch.setattr(cams, "latest_run", lambda: (run, None))
    monkeypatch.setattr(cams, "fetch_series", fake_series)
    first = [("Delhi", "Delhi", 28.635, 77.19), ("Ludhiana", "Punjab", 30.9028, 75.8086)]
    moved = [("Delhi", "Delhi", 28.6351, 77.19), ("Ludhiana", "Punjab", 30.9028, 75.8086)]
    try:
        monkeypatch.setattr(air_jobs, "city_points", lambda _s, _now: first)
        assert air_jobs.cams_step(AT)["rows"] > 0
        monkeypatch.setattr(air_jobs, "city_points", lambda _s, _now: moved)
        assert "rows" in air_jobs.cams_step(AT)  # new points: fetched, values unchanged
        assert air_jobs.cams_step(AT)["skipped"] == "this CAMS run is already stored"
        assert len(fetched) == 2
    finally:
        _drop_fetches(fetched)


def test_dss_snapshot_cites_its_newest_confirming_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-reading an unchanged DSS page keeps one row and first_seen_at, and the response then
    cites the newest read, so the UI's 'fetched N min ago' is true."""
    import blindspot.jobs.air as air_jobs

    data = iitm_dss.parse_dss((FIXTURES / "dss_charts.html").read_text(encoding="utf-8"))
    with session_scope() as session:
        other = session.execute(
            text(
                "SELECT 1 FROM dss_snapshot d LEFT JOIN source_fetch f ON f.id = d.fetch_id "
                "WHERE d.data_sha256 = :sha AND coalesce(f.meta->>'test', '') <> :tag"
            ),
            {"sha": data.sha256(), "tag": TAG},
        ).first()
    if other:
        pytest.skip("this DSS version is stored by someone else; not touching their row")
    reads = [_fake_result(iitm_dss.SOURCE_KEY, iitm_dss.DSS_URL, {}) for _ in range(2)]
    queue = list(reads)
    monkeypatch.setattr(iitm_dss, "fetch_dss", lambda: (data, queue.pop(0)))
    try:
        assert air_jobs.air_dss()["new_version"] is True
        assert air_jobs.air_dss()["new_version"] is False
        with session_scope() as session:
            row = session.execute(
                text(
                    "SELECT count(*) OVER () AS n, fetch_id, first_seen_at, last_seen_at "
                    "FROM dss_snapshot WHERE data_sha256 = :sha"
                ),
                {"sha": data.sha256()},
            ).one()
        assert row.n == 1 and row.fetch_id == reads[1].fetch_id
        assert row.last_seen_at > row.first_seen_at
        body = client.get("/api/air/stubble-share").json()
        assert body["sources"][0]["fetch_id"] == reads[1].fetch_id
    finally:
        _drop_fetches([r.fetch_id for r in reads])


def test_air_job_survives_an_upstream_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAQ and CAMS unreachable: the job records both failures, still returns, and writes no
    value; with every source down it fails (run_job records that). Nothing is ever invented."""
    import blindspot.http as http
    import blindspot.jobs.air as air_jobs
    from blindspot.config import Settings

    dead = "http://127.0.0.1:9"  # a closed local port: a real connection failure
    http.reset_cooldowns()  # a pause left by another test would skip the calls we check
    monkeypatch.setattr(http, "MAX_ATTEMPTS", 1)  # fail fast; retries are http.py's own tests
    monkeypatch.setattr(openaq, "BASE_URL", f"{dead}/v3")
    monkeypatch.setattr(cams, "META_URL", f"{dead}/meta.json")
    monkeypatch.setattr(datagov, "RESOURCE_URL", f"{dead}/resource")
    no_datagov = Settings(openaq_api_key="k" * 40, data_gov_in_api_key=None)
    monkeypatch.setattr(air_jobs, "get_settings", lambda: no_datagov)
    with session_scope() as session:
        before = session.execute(text("SELECT count(*) FROM air_obs")).scalar()
        first_id = session.execute(text("SELECT coalesce(max(id), 0) FROM source_fetch")).scalar()
    try:
        summary = air_jobs.air()
        assert set(summary["errors"]) == {"openaq", "cams"}
        assert summary["datagov"] == {"skipped": "DATA_GOV_IN_API_KEY is not set"}
        assert all("ConnectError" in e for e in summary["errors"].values())
        everything = Settings(openaq_api_key="k" * 40, data_gov_in_api_key="k" * 40)
        monkeypatch.setattr(air_jobs, "get_settings", lambda: everything)
        with pytest.raises(RuntimeError, match="every air-quality source failed"):
            air_jobs.air()
        with session_scope() as session:
            assert session.execute(text("SELECT count(*) FROM air_obs")).scalar() == before
            failed = session.execute(
                text(
                    "SELECT source_key, ok FROM source_fetch WHERE id > :id AND url LIKE :dead"
                ),
                {"id": first_id, "dead": f"{dead}%"},
            ).all()
        assert failed and not any(ok for _key, ok in failed)  # every failed call was recorded
        assert {key for key, _ok in failed} == {OPENAQ, CAMS, DATAGOV}
    finally:
        with session_scope() as session:
            session.execute(
                text("DELETE FROM source_fetch WHERE id > :id AND url LIKE :dead"),
                {"id": first_id, "dead": f"{dead}%"},
            )
        http.reset_cooldowns()


# --- live checks (tiny; skipped without a key or network) ---------------------------------------


def test_live_cams_one_point() -> None:
    try:
        run, _ = cams.latest_run()
        series, result = cams.fetch_series(
            [cams.Point("Ludhiana", 30.9028, 75.8086)], past_days=1, forecast_days=1
        )
    except FetchError as exc:
        pytest.skip(f"Open-Meteo unreachable: {exc}")
    assert run.initialised < run.available and result.fetch_id is not None
    assert series[0].times and any(v is not None for v in series[0].values["pm25"])


def test_live_openaq_one_station(locations: dict[int, openaq.Location]) -> None:
    key = get_settings().openaq_api_key
    if key is None:
        pytest.skip("OPENAQ_API_KEY is not set")
    client = openaq.OpenAQClient(key.get_secret_value())
    try:
        readings, fetch_id = client.latest(locations[5569])  # Ludhiana, PAU
    except FetchError as exc:
        pytest.skip(f"OpenAQ unreachable: {exc}")
    assert fetch_id is not None and {r.parameter for r in readings} <= {"pm25", "pm10"}


def test_live_datagov_shape_and_no_key_in_the_log() -> None:
    key = get_settings().data_gov_in_api_key
    if key is None:
        pytest.skip("DATA_GOV_IN_API_KEY is not set")
    params = {
        "api-key": key.get_secret_value(),
        "format": "json",
        "limit": 5,
        "filters[state]": "Punjab",
    }
    try:
        result = fetch(datagov.SOURCE_KEY, datagov.RESOURCE_URL, params=params)
    except FetchError as exc:
        pytest.skip(f"data.gov.in unreachable: {exc}")
    assert key.get_secret_value() not in result.url_logged and "api-key=***" in result.url_logged
    rows = datagov.parse_records(result.data["records"])
    assert all(r.state == "Punjab" and 0 <= r.value <= 500 for r in rows)
