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
from sqlalchemy import delete, text

from blindspot.analytics import geofire as gf
from blindspot.db import init_db, session_scope
from blindspot.jobs import geostationary as job
from blindspot.models.geofires import (
    GeoFirePixel,
    GeoFireSlot,
    GeoFireStatic,
    GeoFireStaticPixel,
    GeoFireValidation,
)
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


def _flat_night_scene(size: int = 41) -> tuple[np.ndarray, ...]:
    """A clear night: BT11 300 K, BT38 ~305 K (BTD ~5 K, 0.1 K noise), 12 km2 pixels."""
    rng = np.random.default_rng(0)
    bt11 = np.full((size, size), 300.0)
    bt38 = 305.0 + rng.normal(0.0, 0.1, (size, size))
    return bt38, bt11, np.full((size, size), 120.0), np.full((size, size), 12.0)


def test_excluded_pixels_leave_the_background_and_are_reported_apart() -> None:
    """A fire in the middle of a flagged hot block (a plant) must still be found: the flagged
    pixels are left out of its background, so the window grows to the clear fields beyond."""
    bt38, bt11, sza, area = _flat_night_scene()
    block = np.zeros(bt38.shape, dtype=bool)
    block[17:24, 17:24] = True  # 7 x 7 flagged pixels, BTD ~15 K
    bt38[block] = 315.0
    bt38[20, 20] = 310.0  # the fire: BTD 10 K, inside the block's 7 x 7 window
    exclude = block.copy()
    exclude[20, 20] = False

    res = gf.detect_fires(bt38, bt11, sza, area, exclude=exclude)
    assert res.fire[20, 20]
    assert not (res.fire & exclude).any()
    assert res.excluded_fire is not None and res.excluded_fire[exclude].all()
    assert res.stats["excluded_fires"] == int(exclude.sum())
    assert (res.confidence[exclude] >= 1).all()  # kept for audit with the confidence they earned
    assert res.stats["fires"] == 1
    assert res.bg_btd[20, 20] == pytest.approx(5.0, abs=0.3)  # clear fields, not the plant

    # Without the mask the block is one big fire and the pixel is just part of it.
    plain = gf.detect_fires(bt38, bt11, sza, area)
    assert plain.fire[block].all() and plain.excluded_fire is not None
    assert not plain.excluded_fire.any()


def test_window_accounting_adds_up() -> None:
    """A scan on a window boundary counts once: half in the window, half in the gap after it, and
    the gaps add up to exactly the pixel-hours outside the windows."""
    from blindspot.api.routers.geostationary import _gaps

    t = datetime(2026, 11, 17, 11, 17, tzinfo=UTC)  # Aqua over Punjab at 16:47 IST
    p = gf.PolarPass("Aqua", "MODIS", t, t + timedelta(minutes=2), 10, 1.0, 100.0)
    windows = gf.polar_windows([p], pad_minutes=15)
    assert (windows[0]["start"], windows[0]["end"]) == ("16:32", "17:04")
    end = windows[0]["end_min"]
    assert gf.inside_windows(end - 1, windows) and not gf.inside_windows(end, windows)
    spans = gf.window_spans(windows)
    assert gf.scan_overlap(end, spans) == pytest.approx(0.5)
    assert gf.scan_overlap(16 * 60 + 47, spans) == 1.0 and gf.scan_overlap(19 * 60, spans) == 0.0
    # scans every 10 minutes, one exactly on the window's end, counts like an evening peak
    start = datetime(2026, 11, 17, 9, 4, tzinfo=UTC)  # 14:34 IST
    slots = [(start + timedelta(minutes=10 * k), 5 + k) for k in range(40)]
    assert any(gf.ist_minutes(s) == end for s, _ in slots)
    outside = sum(c * (1 - gf.scan_overlap(gf.ist_minutes(s), spans)) for s, c in slots)
    in_gaps = 0.0
    for g in _gaps(windows):
        lo = int(g["start"][:2]) * 60 + int(g["start"][3:])
        in_gaps += sum(c * gf.scan_overlap(gf.ist_minutes(s), [(lo, lo + g["minutes"])])
                       for s, c in slots)  # fmt: skip
    assert in_gaps == pytest.approx(outside)
    # Wider pads leave less outside; windows taken day by day never leave less than one clock.
    other_day = gf.PolarPass("Aqua", "MODIS", t - timedelta(days=1, minutes=40),
                             t - timedelta(days=1, minutes=38), 10, 1.0, 100.0)  # fmt: skip
    shares = gf.outside_shares(slots, [p, other_day], (0, 5, 10, 15))
    unions = [r["union"] for r in shares]
    assert unions == sorted(unions, reverse=True)
    assert all(r["per_day"] >= r["union"] for r in shares)


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


# --- persistent heat sources ----------------------------------------------------------------------

HISTORY_ALG = "test-history"  # GK-2A detections of earlier nights, as any algorithm version stored them
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)  # night of 30 Oct (IST); history: nights 26-29


def _history_pixel(row: int, col: int, t: datetime, lon: float, lat: float) -> GeoFirePixel:
    return GeoFirePixel(
        slot_utc=t, t_utc=t, algorithm=HISTORY_ALG, row=row, col=col, lon=lon, lat=lat,
        confidence=1, absolute=False, geom=f"SRID=4326;POINT({lon} {lat})",
    )  # fmt: skip


@pytest.fixture
def static_setup(scan: gk2a.Scan, polys: dict):
    """A FIRMS static site on one of the scan's fire pixels, and GK-2A night history: a pixel hot
    in the deep night on 2 earlier nights (a plant), one hot only at 19:00 IST on 3 nights (crop
    fires), one hot in the deep night on 1 night only."""
    from blindspot.models.fires import FireStaticSite

    init_db()
    plain = job.analyse(scan, polys)
    geom, w = plain.geom, scan.window
    fi, fj = (int(x[0]) for x in np.nonzero(plain.region_fire))
    site_rc = (w.row0 + fi, w.col0 + fj)
    quiet = np.argwhere(geom.region & ~plain.result.fire & ~plain.result.cloud)
    (pi, pj), (ei, ej), (oi, oj) = quiet[100], quiet[400], quiet[700]
    rc = {
        "plant": (w.row0 + int(pi), w.col0 + int(pj)),
        "evening": (w.row0 + int(ei), w.col0 + int(ej)),
        "once": (w.row0 + int(oi), w.col0 + int(oj)),
    }
    lon, lat = float(geom.lon[fi, fj]), float(geom.lat[fi, fj])
    with session_scope() as session:
        session.add(FireStaticSite(
            area=session.execute(text(
                "SELECT ST_AsEWKT(ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)"
                "::geography, 400)::geometry))"), {"lon": lon, "lat": lat}).scalar_one(),
            lat=lat, lon=lon, nights=5, night_detections=9, first_night=datetime(2026, 10, 1).date(),
            last_night=datetime(2026, 10, 28).date(), computed_at=NOW,
        ))  # fmt: skip
        hist = []
        for name, times in {
            "plant": [datetime(2026, 10, 27, 20, 0), datetime(2026, 10, 28, 19, 30)],  # 01:30, 01:00 IST
            "evening": [datetime(2026, 10, d, 13, 30) for d in (26, 27, 28)],  # 19:00 IST
            "once": [datetime(2026, 10, 28, 20, 0), datetime(2026, 10, 28, 20, 10)],
        }.items():
            r, c = rc[name]
            i, j = r - w.row0, c - w.col0
            hist += [
                _history_pixel(r, c, t.replace(tzinfo=UTC), float(geom.lon[i, j]), float(geom.lat[i, j]))
                for t in times
            ]
        session.add_all(hist)
    yield {"site": site_rc, **rc, "plain": plain}
    with session_scope() as session:
        session.execute(delete(FireStaticSite))
        session.execute(delete(GeoFirePixel).where(GeoFirePixel.algorithm == HISTORY_ALG))
        for table in (GeoFirePixel, GeoFireStaticPixel, GeoFireSlot):
            session.execute(delete(table).where(table.slot_utc == SLOT))
        session.execute(delete(GeoFireStatic))


def test_static_mask_flags_plants_not_evening_fires(scan: gk2a.Scan, polys: dict, static_setup) -> None:
    with session_scope() as session:
        ev = job.load_static_evidence(session, NOW)
    assert ev.as_of_night == datetime(2026, 10, 30).date()
    mask = job.StaticMasks(ev).for_scan(scan, static_setup["plain"].geom)
    assert set(mask.pixels[static_setup["plant"]]) == {"gk2a_night"}
    assert mask.pixels[static_setup["plant"]]["gk2a_night"]["nights"] == 2
    assert "firms" in mask.pixels[static_setup["site"]]
    assert static_setup["evening"] not in mask.pixels  # crop fires burn in the evening, not at 01:00
    assert static_setup["once"] not in mask.pixels  # one night is not persistence
    assert int(mask.exclude.sum()) == len(mask.pixels)
    # The rule reads earlier nights only: as of the night of 28 Oct the plant has 1 night so far.
    with session_scope() as session:
        early = job.load_static_evidence(session, datetime(2026, 10, 29, 6, 0, tzinfo=UTC))
    assert static_setup["plant"] not in early.night_pixels


def test_static_pixels_are_stored_apart_and_left_out_of_counts(
    scan: gk2a.Scan, polys: dict, static_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blindspot.api.main import create_app
    from blindspot.api.routers import geostationary as router

    with session_scope() as session:
        static = job.StaticMasks(job.load_static_evidence(session, NOW))
    a = job.analyse(scan, polys, static)
    plain = static_setup["plain"]
    sr, sc = static_setup["site"]
    i, j = sr - scan.window.row0, sc - scan.window.col0
    assert plain.result.fire[i, j] and not a.result.fire[i, j]
    assert a.result.excluded_fire is not None and a.result.excluded_fire[i, j]
    with session_scope() as session:
        n = job.store_analysis(session, a)
        assert job.store_static_mask(session, a.static, NOW) == len(a.static.pixels)
    assert n == int(a.region_fire.sum()) and int(a.region_static_fire.sum()) >= 1
    with session_scope() as session:
        kept = session.query(GeoFireStaticPixel).filter_by(slot_utc=SLOT).all()
        assert {(p.row, p.col) for p in kept} >= {(sr, sc)}
        assert next(p for p in kept if (p.row, p.col) == (sr, sc)).rules == "firms"
        assert session.query(GeoFireSlot).filter_by(slot_utc=SLOT).one().count == n
        assert {r.rule for r in session.query(GeoFireStatic).all()} == {"firms", "gk2a_night"}

    with TestClient(create_app()) as client:
        body = client.get("/api/geostationary/latest", params={"slot": SLOT.isoformat()}).json()
        assert len(body["fires"]["features"]) == n
        assert body["scan"]["count_static"] == len(kept)
        assert not any(f["properties"]["static"] for f in body["fires"]["features"])
        both = client.get(
            "/api/geostationary/latest",
            params={"slot": SLOT.isoformat(), "include_static": "true"},
        ).json()
        assert len(both["fires"]["features"]) == n + len(kept)
        assert sum(f["properties"]["static"] for f in both["fires"]["features"]) == len(kept)

        monkeypatch.setattr(router, "utcnow", lambda: SLOT + timedelta(hours=12))
        d = client.get("/api/geostationary/diurnal", params={"days": 1}).json()
        d2 = client.get(
            "/api/geostationary/diurnal", params={"days": 1, "include_static": "true"}
        ).json()
        assert [s["count"] for s in d["slots"]] == [n] and d["slots"][0]["count_static"] == len(kept)
        assert d2["slots"][0]["count"] == n + len(kept)
        assert d["static_sources"]["pixels_flagged"] == len(a.static.pixels)
        assert d["static_sources"]["included"] is False and d2["static_sources"]["included"]
        assert d["static_sources"]["pixel_hours"] == round(len(kept) * 10 / 60, 1)
