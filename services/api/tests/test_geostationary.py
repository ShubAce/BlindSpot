"""Geostationary fire watch (GK-2A): reader, fire test, validation and endpoints.

Fixtures under tests/fixtures/geostationary are real recordings:
- scan_20261029_1040.npz: our cached window (sw038 + ir112 counts and calibration attributes) of
  the GK-2A full-disk scan of 2026-10-29 10:40 UTC, as sources/gk2a.py stored it;
- firms_aqua_20261029.csv: the FIRMS detections (fire_detection rows) of Aqua's pass over the
  region at ~10:45 UTC the same day (16:15 IST);
- states_pb_hr.geojson: the Punjab and Haryana polygons of the boundary table (simplified);
- listing_2026102910.xml: the S3 listing of the 10 UTC hour of that day.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import shapely
from fastapi.testclient import TestClient
from sqlalchemy import delete

from blindspot.analytics import geofire as gf
from blindspot.db import init_db, session_scope
from blindspot.jobs import geostationary as job
from blindspot.models.geofires import GeoFirePixel, GeoFireSlot, GeoFireValidation
from blindspot.sources import gk2a

FIX = Path(__file__).parent / "fixtures" / "geostationary"
SLOT = datetime(2026, 10, 29, 10, 40, tzinfo=UTC)


@pytest.fixture(scope="module")
def scan(tmp_path_factory: pytest.TempPathFactory) -> gk2a.Scan:
    """The recorded scan, loaded through the production cache reader."""
    original = gk2a._cache_file
    gk2a._cache_file = lambda slot: FIX / "scan_20261029_1040.npz"  # type: ignore[assignment]
    try:
        loaded = gk2a.load_cached_scan(SLOT, job.CHANNELS)
    finally:
        gk2a._cache_file = original  # type: ignore[assignment]
    assert loaded is not None
    return loaded


@pytest.fixture(scope="module")
def polys() -> dict[str, object]:
    fc = json.loads((FIX / "states_pb_hr.geojson").read_text())
    return {
        f["properties"]["name"]: shapely.geometry.shape(f["geometry"]) for f in fc["features"]
    }


@pytest.fixture(scope="module")
def aqua() -> gk2a.PolarDetections:
    df = pd.read_csv(FIX / "firms_aqua_20261029.csv")
    acq = pd.to_datetime(df.acq_utc, utc=True).dt.tz_localize(None)
    return gk2a.PolarDetections(
        lon=df.lon.to_numpy(float),
        lat=df.lat.to_numpy(float),
        acq_utc=acq.to_numpy(dtype="datetime64[s]"),
        satellite=df.satellite.to_numpy(dtype=object),
        instrument=df.instrument.to_numpy(dtype=object),
        frp_mw=df.frp_mw.to_numpy(float),
        scan_km=df.scan.to_numpy(float),
        track_km=df.track.to_numpy(float),
        source="fire_detection",
    )


def test_listing_parses_and_groups_complete_scans() -> None:
    files = gk2a.parse_listing((FIX / "listing_2026102910.xml").read_text(encoding="utf-8"))
    assert files and all(f.key.startswith("AMI/L1B/FD/202610/29/10/") for f in files)
    scans = gk2a.complete_scans(files, job.CHANNELS)
    assert SLOT in scans
    assert set(scans[SLOT]) == set(job.CHANNELS)
    assert all(s.minute % 10 == 0 for s in scans)


def test_navigation_round_trip_and_view_angle(scan: gk2a.Scan) -> None:
    nav, w = scan.navigation, scan.window
    rr, cc = np.mgrid[w.row0 : w.row1 : 37, w.col0 : w.col1 : 41]
    lon, lat = nav.to_lonlat(rr, cc)
    r2, c2 = nav.to_rowcol(lon, lat)
    assert np.array_equal(r2, rr) and np.array_equal(c2, cc)
    # Punjab sits ~52 deg of longitude west of the 128.2 E sub-point: a very oblique view.
    vza = float(nav.view_zenith_deg(np.array([75.8]), np.array([30.9]))[0])
    assert 60 < vza < 70


def test_brightness_temperatures_are_physical(scan: gk2a.Scan, polys: dict) -> None:
    geom = job.window_geometry(scan, polys)
    bt11 = scan.bt("ir112")[geom.region]
    bt38 = scan.bt("sw038")[geom.region]
    # A late-October afternoon over the Punjab plains: clear land at 11 um ~285-310 K (half the
    # region was under cloud that day, so test the warm end); cloud tops are colder, not absurd.
    assert 285 < np.nanpercentile(bt11, 90) < 312
    assert np.nanmin(bt11) > 200
    assert 285 < np.nanpercentile(bt38, 90) < 335
    # Footprints at this view angle: centres ~4.5 and ~3.6 km apart on a sheared grid, ~12 km2.
    assert 10 < float(np.median(geom.area_km2[geom.region])) < 14


def test_mir_radiance_constant_matches_published_values() -> None:
    # Wooster et al. 2003 publish 3.0e-9 at MODIS's 3.96 um; our fit at 3.96 um gives 2.94e-9.
    assert gf.mir_radiance_constant(3.96) == pytest.approx(3.0e-9, rel=0.05)
    assert gf.MIR_A == pytest.approx(3.12e-9, rel=0.02)


def test_fire_test_agrees_with_aqua_afternoon_pass(
    scan: gk2a.Scan, polys: dict, aqua: gk2a.PolarDetections
) -> None:
    """At 16:15 IST, when fires are at their strongest, GK-2A should find most strong Aqua fires
    and few pixels Aqua did not see."""
    a = job.analyse(scan, polys)
    union = shapely.union_all(list(polys.values()))
    ref = aqua.subset(shapely.contains_xy(union, aqua.lon, aqua.lat))
    rr, cc = scan.navigation.to_rowcol(ref.lon, ref.lat)
    cmp = gf.compare_pass(
        a.result.fire, a.result.clear, a.geom.region,
        rr - scan.window.row0, cc - scan.window.col0, ref.frp_mw, job.STRONG_FRP_MW,
    )  # fmt: skip
    assert cmp.ref_pixels > 50
    assert cmp.geo_pixels > 20
    assert cmp.false_alarm_rate is not None and cmp.false_alarm_rate < 0.15
    assert cmp.ref_hits_strong / cmp.ref_pixels_strong > 0.6


def test_diurnal_helpers() -> None:
    t = datetime(2026, 10, 29, 10, 45, tzinfo=UTC)  # 16:15 IST
    assert gf.ist_minute_of_day(t) == 16 * 60 + 15
    p = gf.PolarPass("Aqua", "MODIS", t, t + timedelta(minutes=2), 10, 1.0, 100.0)
    windows = gf.polar_windows([p], pad_minutes=15)
    assert windows[0]["start"] == "16:00" and windows[0]["end"] == "16:32"
    assert gf.inside_windows(16 * 60 + 20, windows)
    assert not gf.inside_windows(18 * 60, windows)
    hours = gf.hourly_means([(t, 4), (t + timedelta(minutes=10), 6)])
    assert hours[16]["mean_count"] == 5.0 and hours[16]["scans"] == 2


@pytest.fixture
def stored(
    scan: gk2a.Scan, polys: dict, aqua: gk2a.PolarDetections, monkeypatch: pytest.MonkeyPatch
):
    """Store the recorded scan and its validation in the test database with the job's own code."""
    init_db()
    a = job.analyse(scan, polys)
    with session_scope() as session:
        n = job.store_analysis(session, a)
    monkeypatch.setattr(job, "region_polygons", lambda session: polys)
    monkeypatch.setattr(job, "polar_reference", lambda session, start, end: aqua)
    summary = job.validate_range(SLOT - timedelta(hours=1), SLOT + timedelta(hours=1), lambda s: scan)
    yield n, summary
    with session_scope() as session:
        alg = gf.ALGORITHM_VERSION
        session.execute(
            delete(GeoFirePixel).where(GeoFirePixel.slot_utc == SLOT, GeoFirePixel.algorithm == alg)
        )
        session.execute(
            delete(GeoFireValidation).where(
                GeoFireValidation.slot_utc == SLOT, GeoFireValidation.algorithm == alg
            )
        )
        session.execute(
            delete(GeoFireSlot).where(GeoFireSlot.slot_utc == SLOT, GeoFireSlot.algorithm == alg)
        )


def test_job_stores_scan_and_validation(stored) -> None:
    n, summary = stored
    assert n > 20
    assert summary["compared"] == 1 and summary["passes"] == 1
    assert summary["false_alarm_rate"] < 0.15
    with session_scope() as session:
        row = session.query(GeoFireSlot).filter_by(slot_utc=SLOT).one()
        assert row.status == "ok" and row.count == n == row.count_pb + row.count_hr
        assert 0 < row.clear_frac <= 1 and row.sza_deg < 90
        assert session.query(GeoFirePixel).filter_by(slot_utc=SLOT).count() == n


def test_endpoints_follow_the_contract(stored) -> None:
    from blindspot.api.main import create_app

    n, _ = stored
    with TestClient(create_app()) as client:
        r = client.get("/api/geostationary/latest", params={"slot": SLOT.isoformat()})
        assert r.status_code == 200
        body = r.json()
        assert body["generated_at"] and isinstance(body["sources"], list)
        assert body["scan"]["count"] == n
        feats = body["fires"]["features"]
        assert len(feats) == n
        assert feats[0]["geometry"]["type"] == "Polygon"
        assert {"frp_mw", "confidence", "state"} <= set(feats[0]["properties"])

        r = client.get("/api/geostationary/diurnal", params={"days": 7})
        assert r.status_code == 200
        body = r.json()
        for key in (
            "slots", "hourly_ist", "polar_windows_ist", "peak_ist",
            "share_outside_polar_windows", "validation", "sources", "generated_at",
        ):  # fmt: skip
            assert key in body
        assert len(body["hourly_ist"]) == 24
        v = body["validation"]
        assert {"hit_rate", "false_alarm_rate", "n", "groups"} <= set(v)
        assert v["groups"]["modis_aqua_day"]["passes"] >= 1

        assert client.get("/api/geostationary/diurnal", params={"days": 99}).status_code == 422
        missing = client.get("/api/geostationary/latest", params={"slot": "2001-01-01T00:00:00Z"})
        assert missing.status_code == 404
