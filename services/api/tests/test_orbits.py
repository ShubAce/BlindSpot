"""Orbits module tests: TLE parsing, pass geometry, agreement with skyfield and with NASA FIRMS
timestamps, and /api/orbits/*.

The element sets are CelesTrak's real answers recorded on 2026-10-30 17:49 UTC
(tests/fixtures/orbits/); the FIRMS detections are the fires module's recorded files. No test
calls CelesTrak: its usage policy firewalls clients that download too often.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from skyfield.api import EarthSatellite, load, wgs84
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from blindspot.analytics.passes import (
    INSTRUMENTS,
    OrbitElements,
    current_status,
    find_passes,
    firms_label_errors,
    ground_km,
    half_swath_from_scan,
    look_clock,
    passes_for_site,
    summarize_errors,
    sun_elevation_deg,
    track,
)
from blindspot.db import init_db, session_scope
from blindspot.jobs.orbits import refresh_allowed
from blindspot.models.orbits import Tle
from blindspot.sources.celestrak import (
    SATELLITE_BY_KEY,
    SATELLITES,
    TleFormatError,
    orbit_mean_elements,
    parse_tle,
)
from blindspot.sources.firms import PRODUCT_BY_SOURCE, parse_csv

ORBITS = Path(__file__).parent / "fixtures" / "orbits"
FIRES = Path(__file__).parent / "fixtures" / "fires"
SANGRUR = (30.25, 75.84)  # the research doc's test site, in the Punjab burn belt


@pytest.fixture(scope="module", autouse=True)
def database() -> None:
    init_db()


@pytest.fixture(scope="module")
def orbits() -> dict[str, OrbitElements]:
    records = parse_tle((ORBITS / "celestrak_tle_20261030T1749Z.txt").read_text(encoding="utf-8"))
    by_id = {s.norad_id: s for s in SATELLITES}
    return {
        by_id[r.norad_id].name: OrbitElements(
            r.norad_id, by_id[r.norad_id].key, by_id[r.norad_id].name,
            by_id[r.norad_id].instrument, r.line1, r.line2, r.epoch,
        )  # fmt: skip
        for r in records
    }


def test_recorded_tles_parse_and_match_the_catalogue(orbits: dict[str, OrbitElements]) -> None:
    assert set(orbits) == {s.name for s in SATELLITES}
    aqua = orbits["Aqua"]
    assert aqua.norad_id == 27424
    assert abs(aqua.epoch - datetime(2026, 10, 29, 22, 11, 16, 960_000, tzinfo=UTC)) < timedelta(seconds=1)
    omm = orbit_mean_elements(aqua.line1, aqua.line2, "AQUA")
    assert omm["NORAD_CAT_ID"] == 27424 and omm["OBJECT_ID"] == "2002-022A"
    assert omm["MEAN_MOTION"] == pytest.approx(14.62255676)


def test_a_corrupted_tle_fails_its_checksum() -> None:
    text_ = (ORBITS / "celestrak_tle_20261030T1749Z.txt").read_text(encoding="utf-8")
    name, line1, line2 = text_.splitlines()[:3]
    broken = line2[:20] + ("1" if line2[20] != "1" else "2") + line2[21:]  # one digit changed
    with pytest.raises(TleFormatError, match="checksum"):
        parse_tle(f"{name}\n{line1}\n{broken}\n")
    with pytest.raises(TleFormatError):
        parse_tle("No GP data found")  # CelesTrak's 200 answer for an unknown catalogue number


def test_celestrak_refresh_policy() -> None:
    now = datetime(2026, 10, 30, 18, tzinfo=UTC)
    assert refresh_allowed(None, now)
    assert not refresh_allowed(now - timedelta(minutes=119), now)  # one download per 2 h update
    assert refresh_allowed(now - timedelta(hours=2), now)


def test_published_swaths_agree_with_scan_geometry() -> None:
    # MODIS: +/-55 deg at its nominal 705 km gives exactly the published 2330 km swath.
    assert half_swath_from_scan(55.0, 705.0) == pytest.approx(1165.0, abs=1.0)
    # VIIRS +/-56.06 deg and MSI 20.6 deg agree with 3040 km and 290 km within 1.5 %.
    assert half_swath_from_scan(56.06, 833.0) == pytest.approx(1520.0, rel=0.015)
    assert half_swath_from_scan(10.3, 795.0) == pytest.approx(145.0, rel=0.015)
    assert {k: i.half_swath_km for k, i in INSTRUMENTS.items()} == {
        "VIIRS": 1520.0, "MODIS": 1165.0, "MSI": 148.0,  # SYNTHESIS decision, used by the map too
    }  # fmt: skip


def test_fast_ground_track_matches_skyfield(orbits: dict[str, OrbitElements]) -> None:
    ts = load.timescale(builtin=True)
    t0 = datetime(2026, 10, 30, 16, tzinfo=UTC)
    offsets = np.arange(0.0, 3 * 86400.0, 1777.0)
    times = ts.from_datetimes([t0 + timedelta(seconds=float(s)) for s in offsets])
    for orbit in orbits.values():
        fast = track(orbit.satrec(), t0, offsets)
        lat, lon = wgs84.latlon_of(EarthSatellite(orbit.line1, orbit.line2, orbit.name, ts).at(times))
        assert float(np.max(ground_km(fast.lat, fast.lon, lat.degrees, lon.degrees))) < 0.2


def test_passes_over_sangrur_match_the_research_doc(orbits: dict[str, OrbitElements]) -> None:
    # docs/research/firms-and-orbits.md section 3.3, computed independently with skyfield's
    # find_events (and checked against satellite.js): UTC time of closest approach, km.
    expected = [
        ("NOAA-20", datetime(2026, 10, 30, 20, 33, 56, tzinfo=UTC), 363),
        ("NOAA-21", datetime(2026, 10, 30, 21, 17, 16, tzinfo=UTC), 828),
        ("Suomi NPP", datetime(2026, 10, 31, 9, 11, 34, tzinfo=UTC), 1355),
        ("Aqua", datetime(2026, 10, 31, 10, 21, 9, tzinfo=UTC), 72),
        ("Terra", datetime(2026, 10, 31, 15, 7, 14, tzinfo=UTC), 136),
        ("Sentinel-2A", datetime(2026, 11, 1, 5, 40, 45, tzinfo=UTC), 145),
    ]
    start = datetime(2026, 10, 30, 16, tzinfo=UTC)
    found = passes_for_site(orbits.values(), *SANGRUR, start, start + timedelta(hours=48))
    assert len([p for p in found if p.instrument != "MSI"]) == 24  # as listed in the doc
    for name, when, km in expected:
        match = [p for p in found if p.satellite == name and abs(p.peak_utc - when) < timedelta(seconds=2)]
        assert match, f"{name} {when} not predicted"
        assert match[0].min_ground_distance_km == pytest.approx(km, abs=1.0)
        assert match[0].start_utc <= match[0].peak_utc <= match[0].end_utc
    s2 = [p for p in found if p.instrument == "MSI"]
    assert all(p.direction == "descending" and p.daynight == "D" for p in s2)


def test_predicted_times_match_firms_acquisition_labels(orbits: dict[str, OrbitElements]) -> None:
    """The orbit model against NASA's own timestamps: every recorded NOAA-20, Aqua and Terra
    detection (South Asia, 29-30 Oct) must be predicted within 3 minutes of its FIRMS label."""
    checks = []
    for source, file in (("VIIRS_NOAA20_NRT", "J1_VIIRS_C2_South_Asia_24h_20261030T1739Z.csv"),
                         ("MODIS_NRT", "MODIS_C6_1_South_Asia_24h_20261030T1739Z.csv")):  # fmt: skip
        rows = parse_csv((FIRES / file).read_text(encoding="utf-8"), PRODUCT_BY_SOURCE[source]).rows
        for name in {r.satellite for r in rows}:
            mine = [(r.acq_utc, r.lat, r.lon) for r in rows if r.satellite == name]
            checks += firms_label_errors(orbits[name], mine)
    assert len(checks) == 626 + 203
    assert all(c.error_min is not None for c in checks), "a detection matched no predicted pass"
    stats = summarize_errors([c.error_min for c in checks])
    assert stats is not None and stats["within_3_min_share"] == 1.0
    assert abs(stats["mean_min"]) < 1.5
    # every matched detection lies inside its instrument's swath (plus the 50 km matching slack)
    assert max(c.cross_track_km for c in checks) < INSTRUMENTS["VIIRS"].half_swath_km + 50


def test_sun_elevation_tells_day_from_night() -> None:
    lat, lon = SANGRUR
    noon = datetime(2026, 10, 30, 6, 50, tzinfo=UTC)  # 12:20 IST, near solar noon
    assert 40 < sun_elevation_deg(lat, lon, noon) < 50  # 90 - 30.25 - 13.9 (declination) ~ 46
    assert sun_elevation_deg(lat, lon, datetime(2026, 10, 30, 19, tzinfo=UTC)) < -30
    # Sunset (elevation 0) at Sangrur on 30 Oct is about 17:42 IST (research doc 3.4).
    sunset = next(
        datetime(2026, 10, 30, 11, tzinfo=UTC) + timedelta(minutes=m)
        for m in range(0, 120) if sun_elevation_deg(lat, lon, datetime(2026, 10, 30, 11, tzinfo=UTC)
                                                    + timedelta(minutes=m)) <= 0
    )  # fmt: skip
    assert abs(sunset - datetime(2026, 10, 30, 12, 12, tzinfo=UTC)) <= timedelta(minutes=6)


def test_current_status_and_the_look_clock(orbits: dict[str, OrbitElements]) -> None:
    start = datetime(2026, 10, 30, 16, tzinfo=UTC)
    found = passes_for_site(orbits.values(), *SANGRUR, start, start + timedelta(hours=48))
    aqua = next(p for p in found if p.satellite == "Aqua" and p.min_ground_distance_km < 100)
    during = current_status(found, aqua.peak_utc)
    assert {"satellite": "Aqua", "instrument": "MODIS", "until_utc": aqua.end_utc,
            "peak_utc": aqua.peak_utc} in during["watching"]  # fmt: skip
    assert during["blind_for_minutes"] == 0
    after = current_status(found, aqua.end_utc + timedelta(minutes=1))
    assert after["last"]["satellite"] == "Aqua" and after["next"]["at_utc"] > aqua.end_utc
    assert after["blind_for_minutes"] > 0

    clock = look_clock(passes_for_site(orbits.values(), *SANGRUR, start, start + timedelta(days=16)))
    span = clock["blind_span_ist"]
    # The research doc's core claim, recomputed: after Aqua's last daytime look (~16:37 IST) no
    # fire sensor looks until Terra's night pass (~19:54 IST).
    assert span["rule"].startswith("from the end of the last daytime window")
    assert "16:3" in span["start"] or "16:4" in span["start"]
    assert span["end"].startswith("19:5")


@pytest.fixture()
def stored_tles(orbits: dict[str, OrbitElements]) -> object:
    """Fixture TLEs in the test database's tle table; rows this test added are removed after."""
    added = []
    with session_scope() as session:
        for orbit in orbits.values():
            sat = SATELLITE_BY_KEY[orbit.key]
            row = session.execute(
                insert(Tle)
                .values(norad_id=orbit.norad_id, key=orbit.key, name=orbit.name,
                        object_name=sat.object_name, instrument=orbit.instrument,
                        line1=orbit.line1, line2=orbit.line2, epoch=orbit.epoch,
                        fetched_at=orbit.epoch)  # fmt: skip
                .on_conflict_do_nothing()
                .returning(Tle.norad_id)
            ).scalar()
            if row is not None:
                added.append(row)
    yield orbits
    with session_scope() as session:
        session.execute(text("DELETE FROM tle WHERE norad_id = ANY(:ids)"), {"ids": added})


def test_orbit_endpoints_follow_the_contract(stored_tles: dict[str, OrbitElements]) -> None:
    from blindspot.api.main import create_app

    with TestClient(create_app()) as client:
        tle = client.get("/api/orbits/tle").json()
        sats = {s["name"]: s for s in tle["satellites"]}
        assert set(sats) == {s.name for s in SATELLITES}
        for s in sats.values():
            assert {"key", "name", "norad_id", "instrument", "swath_km", "line1", "line2", "epoch",
                    "fetched_at"} <= set(s)  # fmt: skip
            assert len(s["line1"]) == len(s["line2"]) == 69 and s["omm"]["NORAD_CAT_ID"] == s["norad_id"]

        passes = client.get("/api/orbits/passes", params={"lat": SANGRUR[0], "lon": SANGRUR[1],
                                                          "hours_back": 24, "hours_ahead": 48}).json()  # fmt: skip
        assert passes["site"]["lat"] == SANGRUR[0] and passes["site"]["default"] is False
        assert passes["passes"], "no passes in 72 h is impossible for eight polar orbiters"
        p = passes["passes"][0]
        assert {"satellite", "instrument", "start_utc", "peak_utc", "end_utc", "peak_elevation_deg",
                "min_ground_distance_km"} <= set(p)  # fmt: skip
        assert {"watching", "next"} <= set(passes["current"])
        if passes["current"]["next"]:
            assert {"satellite", "at_utc"} <= set(passes["current"]["next"])

        default = client.get("/api/orbits/passes").json()
        assert default["site"]["default"] is True and default["site"]["name"].startswith("Punjab")
        clock = client.get("/api/orbits/clock", params={"days": 2}).json()
        assert clock["looks"] and "blind_span_ist" in clock
        validation = client.get("/api/orbits/validation", params={"days": 1}).json()
        assert "overall" in validation and "satellites" in validation
        for body in (tle, passes, default, clock, validation):
            assert body["generated_at"].endswith("Z") and body["sources"]
        assert any(s["key"] == "celestrak" for s in passes["sources"])

        assert client.get("/api/orbits/passes", params={"lat": 30.0}).status_code == 400
        assert client.get("/api/orbits/passes", params={"instruments": "LIDAR"}).status_code == 400
        assert client.get("/api/orbits/passes", params={"lat": 95, "lon": 75}).status_code == 422


def test_find_passes_drops_msi_at_night(orbits: dict[str, OrbitElements]) -> None:
    start = datetime(2026, 10, 30, 0, tzinfo=UTC)
    s2 = find_passes(orbits["Sentinel-2B"], *SANGRUR, start, start + timedelta(days=10))
    assert s2 and all(p.daynight == "D" and p.direction == "descending" for p in s2)
