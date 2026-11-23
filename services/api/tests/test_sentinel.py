"""Tests for the Sentinel-2 module: STAC parsing, tile ownership, the burn and harvest rules, cluster
stitching, fire matching and the /api/scars routes.

Pixel tests run on recorded real data only (tests/fixtures/sentinel/): a 64 x 64 px (1.28 km) window
of tile 43REQ near Moga read from the Earth Search COGs for the passes of 18, 23 and 28 October 2026,
the WorldCover classes of the same window, and Planetary Computer's copy of the 28 October pass,
which is corrupt upstream (SYNTHESIS item 2b) and must be rejected. The window is centred on a
field that was standing crop on 18 Oct, harvested by 23 Oct and burnt by 28 Oct (checked visually).
Database tests write only their own rows and delete them again.
"""

from __future__ import annotations

import datetime as dt
import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pystac
import pytest
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import box
from sqlalchemy import text

from blindspot.analytics import burnscar, harvest
from blindspot.analytics.burnscar import RULE, BlockState
from blindspot.db import SessionLocal, init_db
from blindspot.sources import stac

FIXTURES = Path(__file__).parent / "fixtures" / "sentinel"
META = json.loads((FIXTURES / "window_43REQ.json").read_text())
PASSES = [p["item_id"] for p in META["passes"]]
DAYS = [stac.day_number(dt.datetime.fromisoformat(p["datetime"])) for p in META["passes"]]
X0, Y0, X1, Y1 = META["bounds"]
TRANSFORM = from_origin(X0, Y1, 20, 20)
SHAPE = (64, 64)
TEST_TILE = "TEST1"  # an mgrs_tile value no real row uses, so cleanup cannot touch real data


@pytest.fixture(scope="module")
def window() -> dict[str, np.ndarray]:
    with np.load(FIXTURES / "window_43REQ.npz") as data:
        return {k: data[k] for k in data.files}


def _refl(window: dict[str, np.ndarray], prefix: str) -> dict[str, np.ndarray]:
    # Earth Search c1 (baseline 05.13): scale 0.0001, offset -0.1, as its raster:bands say.
    return {b: stac.reflectance(window[f"{prefix}__{b}"], 1e-4, -0.1) for b in stac.REFLECTANCE_BANDS}


def _scope(window: dict[str, np.ndarray]) -> np.ndarray:
    return window["worldcover"] == stac.WORLDCOVER_CROPLAND


def _run_passes(window: dict[str, np.ndarray]) -> tuple[BlockState, list[str], list[burnscar.PassResult]]:
    state = BlockState.empty(SHAPE)
    registry: list[str] = []
    item_index = np.zeros(SHAPE, np.uint8)
    out = []
    for item_id, day in zip(PASSES, DAYS, strict=True):
        out.append(burnscar.apply_pass(
            state, _refl(window, item_id), window[f"{item_id}__SCL"], [item_id], item_index, day,
            _scope(window), registry))
    return state, registry, out


# --- STAC item and tile grid ----------------------------------------------------------------------


def test_parse_item_reads_grid_scale_and_assets() -> None:
    item = pystac.Item.from_dict(json.loads((FIXTURES / "stac_item_43REQ_20261028.json").read_text()))
    scene = stac.parse_item(item)
    assert scene.item_id == "S2B_T43REQ_20261028T054808_L2A"
    assert scene.mgrs_tile == "43REQ" and scene.epsg == 32643
    assert scene.relative_orbit == 5  # parsed from s2:product_uri (_R005_)
    assert scene.shape20 == (5490, 5490)
    assert scene.transform20[:3] == (20.0, 0.0, 499980.0)
    assert set(scene.hrefs) == {*stac.REFLECTANCE_BANDS, "SCL"}
    assert scene.scale["B12"] == pytest.approx(1e-4) and scene.offset["B12"] == pytest.approx(-0.1)
    assert scene.obs_date == dt.date(2026, 10, 28)


def test_tile_owns_exactly_its_100km_square() -> None:
    item = pystac.Item.from_dict(json.loads((FIXTURES / "stac_item_43REQ_20261028.json").read_text()))
    grid = stac.TileGrid.from_scene(stac.parse_item(item), 20)
    assert grid.core == (500000, 3400000, 600000, 3500000)
    first = grid.core_mask(Window(0, 0, 4, 4))
    # Column 0 (x 499980-500000) belongs to the tile to the west; rows 0-1 to the tile to the north.
    assert not first[:, 0].any() and not first[:2, :].any() and first[2:, 1:].all()
    blocks = grid.blocks(1024)
    assert all(box(*grid.window_bounds(w)).intersects(box(*grid.core)) for w in blocks)
    owned = sum(int(grid.core_mask(w).sum()) for w in blocks)
    assert owned == 5000 * 5000  # 100 km / 20 m, squared: every owned pixel exactly once
    coarse = stac.TileGrid.from_scene(stac.parse_item(item), 40)
    assert (coarse.width, coarse.height) == (2745, 2745)


def test_reflectance_offset_and_nodata() -> None:
    dn = np.array([0, 1000, 2500], dtype=np.uint16)
    out = stac.reflectance(dn, 1e-4, -0.1)
    assert np.isnan(out[0]) and out[1] == pytest.approx(0.0, abs=1e-6) and out[2] == pytest.approx(0.15, abs=1e-6)


# --- the rules on real pixels --------------------------------------------------------------------


def test_burn_found_between_the_hazy_and_the_clear_pass(window) -> None:
    state, registry, results = _run_passes(window)
    first, hazy, clear = results
    assert first.used and first.burn is not None and not first.burn.any()  # nothing to compare yet
    assert hazy.used and hazy.hazy  # 23 Oct: smoke haze (cropland B02 median rose > 0.03)
    assert clear.used and not clear.hazy
    burnt = clear.burn
    assert burnt is not None and burnt[32, 32]  # the field at the window centre burnt 23 -> 28 Oct
    parts = burnscar.extract_parts(burnt, clear.pre_day, TRANSFORM,
                                   {k: np.nan_to_num(v) for k, v in clear.evidence.items()},
                                   {"pre_item": clear.pre_item, "post_item": clear.post_item})
    big = [p for p in parts if p.n_px >= RULE.min_px(20)]
    centre = next(p for p in big if p.geom.contains(box(X0 + 32 * 20, Y1 - 33 * 20, X0 + 33 * 20, Y1 - 32 * 20)))
    assert centre.n_px * 0.04 > 2.0  # hectares
    assert registry[centre.mode("pre_item")] == PASSES[1] and registry[centre.mode("post_item")] == PASSES[2]
    # The evidence is what the rule says it is: darker in NIR and SWIR, charred now.
    assert centre.mean("d_b8a") > RULE.d_b8a and centre.mean("d_b11") > RULE.d_b11
    assert centre.mean("tbi_post") <= RULE.tbi_max and centre.mean("bais2_post") >= RULE.bais2_min
    assert state.arrays["burn_day"][32, 32] == DAYS[2]


def test_hazy_pass_neither_harvests_nor_moves_ndvi_memory(window) -> None:
    state, _registry, results = _run_passes(window)
    assert results[1].hazy and results[1].stats["harvested_px"] == 0  # hazy pass: no harvest verdicts
    # Harvests seen on 28 Oct were judged against the 18 Oct NDVI: the hazy 23 Oct NDVI never
    # entered the memory, so harv_after (last standing view) is 18 Oct, not 23 Oct.
    a = state.arrays
    harvested = a["harv_before"] == DAYS[2]
    assert results[2].stats["harvested_px"] == int(harvested.sum()) > 0
    assert (a["harv_after"][harvested] == DAYS[0]).all()
    assert (a["ndvi_day"][_scope(window) & (a["last_day"] == DAYS[2])] == DAYS[2]).all()
    # The centre field was cut and burnt between the two clear views: its SWIR fell (char) instead
    # of rising (straw), so it is recorded as burnt, and is not a burn-risk candidate.
    assert a["burn_day"][32, 32] == DAYS[2]
    candidates = burnscar.harvest_candidates(state, _scope(window), DAYS[2])
    assert not candidates[32, 32]
    assert not (candidates & (a["burn_day"] > 0)).any()


def test_harvest_rule_needs_ndvi_drop_and_swir_rise(window) -> None:
    pre, post = _refl(window, PASSES[0]), _refl(window, PASSES[2])
    flagged = harvest.harvested_between(burnscar.ndvi(pre), burnscar.ndvi(post), pre["B11"], post["B11"],
                                        np.ones(SHAPE, bool))
    assert flagged.any()
    dn = burnscar.ndvi(pre)[flagged] - burnscar.ndvi(post)[flagged]
    assert (dn > 0.25).all() and (burnscar.ndvi(post)[flagged] < 0.35).all()
    assert (post["B11"][flagged] > pre["B11"][flagged]).all()


def test_corrupt_planetary_computer_pass_fails_the_gate(window) -> None:
    state, registry, _ = _run_passes(window)
    before = {k: v.copy() for k, v in state.arrays.items()}
    pc = {b: stac.reflectance(window[f"PC__{b}"], 1e-4, -0.1) for b in stac.REFLECTANCE_BANDS}
    out = burnscar.apply_pass(state, pc, window["PC__SCL"], [META["pc_pass"]["item_id"]],
                              np.zeros(SHAPE, np.uint8), DAYS[2] + 1, _scope(window), registry)
    assert not out.used and "implausible" in (out.reason or "")
    for name, array in before.items():  # memory untouched
        np.testing.assert_array_equal(array, state.arrays[name])


def test_state_round_trip(window, tmp_path) -> None:
    state, _, _ = _run_passes(window)
    path = tmp_path / "r0_c0.npz"
    state.save(path)
    back = BlockState.load(path, SHAPE)
    assert back.applied_days == state.applied_days
    for name in burnscar.STATE_FIELDS:
        np.testing.assert_array_equal(back.arrays[name], state.arrays[name])
    with pytest.raises(ValueError):
        BlockState.load(path, (32, 32))


def test_clusters_cut_by_a_block_edge_are_stitched(window) -> None:
    _, _, results = _run_passes(window)
    clear = results[2]
    keys = clear.pre_day
    values = {"d_b12": np.nan_to_num(clear.evidence["d_b12"])}
    whole = burnscar.extract_parts(clear.burn, keys, TRANSFORM, values, {})
    cut = 32  # split the window down the middle, through the burnt field
    left = burnscar.extract_parts(clear.burn[:, :cut], keys[:, :cut], TRANSFORM, {"d_b12": values["d_b12"][:, :cut]}, {})
    right_tf = from_origin(X0 + cut * 20, Y1, 20, 20)
    right = burnscar.extract_parts(clear.burn[:, cut:], keys[:, cut:], right_tf, {"d_b12": values["d_b12"][:, cut:]}, {})
    merged = burnscar.merge_parts(left + right)
    assert sorted(p.n_px for p in merged) == sorted(p.n_px for p in whole)
    assert sum(p.sums["d_b12"] for p in merged) == pytest.approx(sum(p.sums["d_b12"] for p in whole), rel=1e-6)


def test_confidence_is_monotone_and_penalised() -> None:
    base = burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=5)
    assert burnscar.confidence(0.22, 0.06, 40, 13, hazy=False, gap_days=5) > base  # darker char
    assert burnscar.confidence(0.30, 0.10, 40, 13, hazy=False, gap_days=5) > base  # stronger darkening
    assert burnscar.confidence(0.30, 0.06, 40, 13, hazy=True, gap_days=5) < base
    assert burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=6) < base
    assert 0.0 <= burnscar.confidence(0.40, 0.0, 1, 13, hazy=True, gap_days=9) <= 1.0


def test_an_ordinary_five_day_revisit_is_not_a_gap() -> None:
    """The gap factor is for a missed view (6 days or more), judged in whole days. _finish_tile
    passes the gap between the two passes' sensing times, and same-orbit revisits drift by about a
    second: the fixture's 23 -> 28 Oct pair is 5.000012 days apart and must score as a 5-day pair."""
    after, before = (dt.datetime.fromisoformat(META["passes"][i]["datetime"]) for i in (1, 2))
    gap = (before - after).total_seconds() / 86400
    assert 5 < gap < 5.001
    five = burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=5)
    assert burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=gap) == five
    assert burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=4.99) == five
    # Real 6-day gaps keep the factor, whichever way their seconds drift (6.01 d is stored).
    six = burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=6)
    assert six == pytest.approx(five * 0.85, abs=1e-3) and six < five
    for drift in (5.99, 6.01):
        assert burnscar.confidence(0.30, 0.06, 40, 13, hazy=False, gap_days=drift) == six


# --- database: matching, names and routes ---------------------------------------------------------


@pytest.fixture(scope="module")
def stored_scar(window) -> dict[str, object]:
    """Insert the real cluster from the fixture as a burn_cluster row (plus its two s2_scene rows)
    in the test database, and remove exactly those rows afterwards."""
    from pyproj import Transformer
    from shapely import transform as shp_transform

    from blindspot.jobs import sentinel as job

    init_db()
    _, registry, results = _run_passes(window)
    clear = results[2]
    parts = burnscar.extract_parts(clear.burn, clear.pre_day, TRANSFORM,
                                   {k: np.nan_to_num(v) for k, v in clear.evidence.items()},
                                   {"pre_item": clear.pre_item, "post_item": clear.post_item})
    part = max(parts, key=lambda p: p.n_px)
    to_ll = Transformer.from_crs(32643, 4326, always_xy=True)
    poly = job._clean_polygon(part.geom, 20)
    ll = shp_transform(poly, lambda xy: np.column_stack(to_ll.transform(xy[:, 0], xy[:, 1])))
    after = dt.datetime.fromisoformat(META["passes"][1]["datetime"])
    before = dt.datetime.fromisoformat(META["passes"][2]["datetime"])
    row = {
        "geom": job._ewkt(ll), "centroid": job._ewkt(job._anchor(ll)), "area_ha": part.n_px * 0.04,
        "n_px": part.n_px, "res_m": 20, "mgrs_tile": TEST_TILE, "scope": "hotspots", "block": "r0_c0",
        "burn_after": after, "burn_before": before, "gap_days": 5.0, "scene_pre": PASSES[1],
        "scene_post": PASSES[2], "collection": stac.E84_COLLECTION, "hazy": False,
        "dnbr": part.mean("dnbr"), "nbr_post": part.mean("nbr_post"), "d_b8a": part.mean("d_b8a"),
        "d_b11": part.mean("d_b11"), "d_b12": part.mean("d_b12"), "bais2_post": part.mean("bais2_post"),
        "tbi_post": part.mean("tbi_post"), "ndvi_pre": part.mean("ndvi_pre"), "ndvi_post": part.mean("ndvi_post"),
        "confidence": burnscar.confidence(part.mean("tbi_post"), part.mean("d_b12"), part.n_px, 13,
                                          hazy=False, gap_days=5.0),
        "model_version": RULE.version,
    }
    from sqlalchemy import insert

    from blindspot.models.sentinel import BurnCluster

    with SessionLocal() as session:
        new_id = session.execute(insert(BurnCluster).returning(BurnCluster.id), [row]).scalar_one()
        for p in META["passes"][1:]:
            session.execute(text(
                "INSERT INTO s2_scene (item_id, collection, datetime, obs_date, mgrs_tile, status) "
                "VALUES (:i, :c, :t, :d, :tile, 'processed') ON CONFLICT (item_id) DO NOTHING"),
                {"i": p["item_id"], "c": stac.E84_COLLECTION, "t": p["datetime"],
                 "d": dt.datetime.fromisoformat(p["datetime"]).date(), "tile": TEST_TILE})
        session.commit()
    yield {"id": new_id, "lon_lat": (ll.centroid.x, ll.centroid.y), "after": after, "before": before}
    with SessionLocal() as session:
        session.execute(text("DELETE FROM burn_cluster WHERE mgrs_tile = :t"), {"t": TEST_TILE})
        session.execute(text("DELETE FROM s2_scene WHERE mgrs_tile = :t"), {"t": TEST_TILE})
        session.commit()


def test_fire_matching_on_a_recorded_firms_confirmed_scar() -> None:
    """A scar the pipeline found near Tarn Taran/Amritsar, and the real FIRMS rows around it (both
    recorded from the dev database, tests/fixtures/sentinel/matched_scar.json). match_fires must
    count exactly the detections inside the scar's window and match radius. Everything runs inside
    a transaction that is rolled back, so other tests never see these rows."""
    from blindspot.jobs import sentinel as job

    rec = json.loads((FIXTURES / "matched_scar.json").read_text())
    with SessionLocal() as session:
        if session.execute(text("SELECT to_regclass('public.fire_detection')")).scalar() is None:
            pytest.skip("fire_detection table not created by the fires module in this database")
        cols = [k for k in rec["fires"][0] if k != "in_window"]
        for fire in rec["fires"]:
            session.execute(text(
                f"INSERT INTO fire_detection ({', '.join(cols)}, geom) VALUES "
                f"({', '.join(':' + c for c in cols)}, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) "
                "ON CONFLICT DO NOTHING"), {c: fire[c] for c in cols})
        scar_id = session.execute(text(
            "INSERT INTO burn_cluster (geom, centroid, area_ha, n_px, res_m, mgrs_tile, scope, block, burn_after, "
            "burn_before, gap_days, scene_pre, scene_post, collection, hazy, confidence, model_version) VALUES "
            "(ST_GeomFromText(:wkt, 4326), ST_PointOnSurface(ST_GeomFromText(:wkt, 4326)), :area, :n, 20, :tile, "
            "'hotspots', 'r0_c0', :after, :before, 5, :pre, :post, :col, false, 0.5, :ver) RETURNING id"),
            {**rec["scar"], "tile": TEST_TILE, "ver": RULE.version}).scalar_one()
        out = job.match_fires(session)
        again = job.match_fires(session)  # nothing changed in between: no row may be rewritten
        n, unseen, nearest = session.execute(text(
            "SELECT fire_matches, unseen, nearest_fire_m FROM burn_cluster WHERE id = :i"), {"i": scar_id}).one()
        # Independent check in Python: UTM 43N distances to the polygon, the window and the radius.
        expected, nearest_py = _expected_matches(session, rec["scar"])
        days, _basis = job.covered_days(session)
        session.rollback()
    after = dt.datetime.fromisoformat(rec["scar"]["after"]).date()
    end = (dt.datetime.fromisoformat(rec["scar"]["before"]) + dt.timedelta(hours=3)).date()
    window_days = {after + dt.timedelta(days=i) for i in range((end - after).days + 1)}
    if not window_days <= set(days):
        # This database holds no complete fire record for some day of the window: no verdict.
        assert n is None and unseen is None
        return
    assert out.get("verdicts_changed", 0) >= 1
    assert again["verdicts_changed"] == 0 and again["verdicts_withdrawn_no_coverage"] == 0
    assert expected >= 1  # the recorded detections explain this scar
    assert n == expected and unseen is False
    assert nearest == pytest.approx(nearest_py, abs=2.0)


def test_new_rows_arrive_named() -> None:
    """store_tile writes each new row once, with its place names already set (through a temporary
    copy of the table), instead of inserting it and then rewriting it once per name. Uses the
    recorded Tarn Taran/Amritsar scar; rolled back, so no other test sees the row."""
    from shapely import wkt

    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    poly = wkt.loads(scar["wkt"])
    row = {
        "geom": f"SRID=4326;{poly.wkt}", "centroid": f"SRID=4326;{poly.representative_point().wkt}",
        "area_ha": scar["area"], "n_px": scar["n"], "res_m": 20, "mgrs_tile": TEST_TILE, "scope": "hotspots",
        "block": "r0_named", "burn_after": dt.datetime.fromisoformat(scar["after"]),
        "burn_before": dt.datetime.fromisoformat(scar["before"]), "gap_days": 5.0, "scene_pre": scar["pre"],
        "scene_post": scar["post"], "collection": scar["col"], "hazy": False, "confidence": 0.5,
        "model_version": RULE.version, "fetch_id": None,
    }
    with SessionLocal() as session:
        district = session.execute(text(
            "SELECT state, name FROM boundary WHERE level = 'district' "
            "AND ST_Intersects(geom, ST_PointOnSurface(ST_GeomFromText(:w, 4326)))"), {"w": scar["wkt"]}).first()
        if district is None:  # the test database has no boundaries: a stand-in district around the scar
            session.execute(text(
                "INSERT INTO boundary (level, name, state, geom, source) VALUES ('district', 'Test district', "
                "'Test state', ST_Multi(ST_Expand(ST_GeomFromText(:w, 4326), 0.01)), 'test')"), {"w": scar["wkt"]})
            district = ("Test state", "Test district")
        # Differences, not totals: the view also holds this pooled connection's unflushed counts.
        writes = text("SELECT n_tup_ins, n_tup_upd FROM pg_stat_xact_user_tables WHERE relname = 'burn_cluster'")
        before = session.execute(writes).one()
        job._replace_rows(session, BurnCluster, None, {}, [row])
        after = session.execute(writes).one()
        got = session.execute(text(
            "SELECT id, state, district, created_at, (SELECT last_value FROM burn_cluster_id_seq) AS seq "
            "FROM burn_cluster WHERE mgrs_tile = :t AND block = 'r0_named'"), {"t": TEST_TILE}).all()
        session.rollback()
    assert len(got) == 1
    assert (got[0].state, got[0].district) == tuple(district)
    assert got[0].id == got[0].seq and got[0].created_at is not None  # the real table's id sequence and defaults
    # Named before it landed: one INSERT and no UPDATE on burn_cluster.
    assert (after.n_tup_ins - before.n_tup_ins, after.n_tup_upd - before.n_tup_upd) == (1, 0)


def _expected_matches(session, scar: dict) -> tuple[int, float]:
    from pyproj import Transformer
    from shapely import transform as shp_transform
    from shapely import wkt
    from shapely.geometry import Point

    to_utm = Transformer.from_crs(4326, 32643, always_xy=True)
    poly = shp_transform(wkt.loads(scar["wkt"]), lambda xy: np.column_stack(to_utm.transform(xy[:, 0], xy[:, 1])))
    after = dt.datetime.fromisoformat(scar["after"])
    end = dt.datetime.fromisoformat(scar["before"]) + dt.timedelta(hours=3)
    rows = session.execute(text(
        "SELECT lon, lat, acq_utc, scan, track FROM fire_detection f "
        "WHERE f.geom && ST_Expand(ST_GeomFromText(:wkt, 4326), 0.05)"), {"wkt": scar["wkt"]}).all()
    count, nearest = 0, float("inf")
    for lon, lat, acq, scan, track in rows:
        if not after <= acq <= end:
            continue
        d = poly.distance(Point(*to_utm.transform(lon, lat)))
        nearest = min(nearest, d)
        if d <= max(1000.0, 500.0 * float(np.hypot(scan or 0, track or 0)) + 200.0):
            count += 1
    return count, nearest


@pytest.fixture(scope="module")
def client() -> TestClient:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def test_scars_endpoint_contract(client, stored_scar) -> None:
    r = client.get("/api/scars", params={"since": "2026-10-28", "until": "2026-10-28", "limit": 20000})
    assert r.status_code == 200
    body = r.json()
    assert body["generated_at"].endswith("Z") and isinstance(body["sources"], list)
    assert {"stac_e84", "s2_cog", "worldcover", "firms"} <= {s["key"] for s in body["sources"]}
    assert set(body["totals"]) >= {"clusters", "area_ha", "unseen_clusters", "unseen_area_ha"}
    fc = body["features"]
    assert fc["type"] == "FeatureCollection"
    ours = next(f for f in fc["features"] if f["properties"]["id"] == stored_scar["id"])
    assert ours["geometry"]["type"] == "Polygon"
    props = ours["properties"]
    for key in ("id", "area_ha", "burn_after", "burn_before", "district", "subdistrict", "village", "centroid",
                "dnbr", "confidence", "unseen", "fire_matches", "scene_pre", "scene_post"):
        assert key in props
    assert props["burn_before"].endswith("Z") and len(props["centroid"]) == 2
    assert client.get("/api/scars", params={"since": "2026-10-29"}).json()["features"]["features"] == [] or all(
        f["properties"]["burn_before"] >= "2026-10-29" for f in
        client.get("/api/scars", params={"since": "2026-10-29"}).json()["features"]["features"])
    assert client.get("/api/scars", params={"bbox": "1,2,3"}).status_code == 400


def test_scar_case_file(client, stored_scar) -> None:
    r = client.get(f"/api/scars/{stored_scar['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["case"]["id"] == stored_scar["id"] and body["case"]["geometry"]["type"] == "Polygon"
    for side, item in (("pre", PASSES[1]), ("post", PASSES[2])):
        img = body["imagery"][side]
        assert img["item_id"] == item and img["collection"] == "sentinel-2-c1-l2a"
        assert img["date"] == item.split("_")[2][:8][:4] + "-" + item.split("_")[2][4:6] + "-" + item.split("_")[2][6:8]
        for kind in ("truecolor", "swir", "nbr"):
            url = img[f"{kind}_url"]
            assert "/api/imagery/render?" in url and f"item={item}" in url and f"kind={kind}" in url
            assert "collection=sentinel-2-c1-l2a" in url and "bbox=" in url
    assert isinstance(body["fires_nearby"], list)
    assert "available" in body["weather_at_burn"]
    # provenance: the case's own two scenes, never the newest catalogue fetch (another tile's item)
    stac_urls = [s["url"] for s in body["sources"] if s["key"] in ("stac_e84", "stac_pc")]
    assert stac_urls == [f"https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a/items/{i}"
                         for i in (PASSES[1], PASSES[2])]
    assert not [s for s in body["sources"] if s["key"] == "s2_cog" and s.get("fetch_id") is None]
    assert client.get("/api/scars/987654321098").status_code == 404


def test_summary_contract(client, stored_scar) -> None:
    body = client.get("/api/scars/summary", params={"since": "2026-10-01"}).json()
    season = body["season"]
    for key in ("since", "clusters", "area_ha", "unseen_area_ha", "unseen_share", "fire_detections"):
        assert key in season
    assert season["clusters"] >= 1
    assert isinstance(body["by_district"], list)
    for row in body["by_district"]:
        assert {"district", "state", "area_ha", "clusters", "unseen_area_ha", "unseen_share",
                "fire_detections"} <= set(row)
    assert body["generated_at"].endswith("Z")


def test_lookalike_filter_and_audit_interval() -> None:
    """Clusters whose mean post-burn NBR is >= 0 (flooded fields, cloud shadow: SWIR-2 below NIR)
    are dropped; char keeps NBR negative. The recorded burnt field at the window centre passes."""
    assert RULE.keeps(-0.2) and RULE.keeps(None)
    assert not RULE.keeps(0.0) and not RULE.keeps(0.08)
    assert RULE.pixel_version == RULE.version == "s2burn-rules-4h"  # the haze correction is per pixel
    # Wilson interval: 11/11 is not "100 % +- 0"; the lower bound is n / (n + z^2).
    lo, hi = burnscar.wilson_interval(11, 11)
    assert hi == 1.0 and lo == pytest.approx(11 / (11 + 1.96**2), abs=1e-3)
    assert burnscar.wilson_interval(0, 0) is None
    audit = burnscar.audit_summary()
    # The audit judged rules-2 clusters; the site says so while the rule is newer (brief/read.ts).
    assert audit["current"] is (burnscar.AUDIT["model_version"] == RULE.version)
    assert 0 < audit["candidate_unseen"]["precision_ci95"][0] <= audit["candidate_unseen"]["precision"] <= 1


def test_published_precision_leaves_out_the_development_sample() -> None:
    """The first audit sample (a3) designed the look-alike filter and was re-scored under it, so the
    candidate-unseen precision the site quotes counts only the held-out samples (15 of 18). The pooled
    figure (20 of 23) stays beside it, for the record."""
    unseen = burnscar.audit_summary()["candidate_unseen"]
    assert (unseen["burnt"], unseen["audited"]) == (15, 18) and unseen["precision"] == pytest.approx(0.833)
    assert unseen["precision_ci95"] == burnscar.wilson_interval(15, 18) == (0.608, 0.942)
    assert sum(t["audited"] for t in unseen["by_tile"].values()) == 18
    assert sum(t["burnt"] for t in unseen["by_tile"].values()) == 15
    pooled = unseen["with_development_sample"]
    assert (pooled["burnt"], pooled["audited"], pooled["precision"]) == (20, 23, 0.87)


def test_burnt_centre_field_survives_the_lookalike_filter(window) -> None:
    _state, _registry, results = _run_passes(window)
    clear = results[2]
    parts = burnscar.extract_parts(clear.burn, clear.pre_day, TRANSFORM,
                                   {k: np.nan_to_num(v) for k, v in clear.evidence.items()},
                                   {"pre_item": clear.pre_item, "post_item": clear.post_item})
    centre = next(p for p in parts if p.geom.contains(box(X0 + 32 * 20, Y1 - 33 * 20, X0 + 33 * 20, Y1 - 32 * 20)))
    assert centre.mean("nbr_post") < 0 and RULE.keeps(centre.mean("nbr_post"))


def test_scar_labels_say_candidate(client, stored_scar) -> None:
    body = client.get("/api/scars", params={"since": "2026-10-28", "until": "2026-10-28", "limit": 20000}).json()
    assert body["method"]["label"] == "candidate unseen burn"
    assert "precision_ci95" in body["method"]["audit"]["candidate_unseen"]
    for f in body["features"]["features"]:
        assert f["properties"]["label"].startswith("candidate")


KEEP_TILE = "TEST2"  # the stored-row tests' own tile: other tests' fixtures use TEST_TILE


def _scar_row(scar: dict, block: str, shift_km: float = 0.0, **changes) -> dict:
    """One burn_cluster row as _finish_tile builds it (the recorded Tarn Taran/Amritsar scar), its
    outline moved `shift_km` east (another field, so another scar)."""
    from shapely import affinity, wkt

    poly = affinity.translate(wkt.loads(scar["wkt"]), xoff=shift_km / 96.0)  # ~96 km per degree at 31 N
    return {
        "geom": f"SRID=4326;{poly.wkt}", "centroid": f"SRID=4326;{poly.representative_point().wkt}",
        "area_ha": scar["area"], "n_px": scar["n"], "res_m": 20, "mgrs_tile": KEEP_TILE, "scope": "hotspots",
        "block": block, "burn_after": dt.datetime.fromisoformat(scar["after"]),
        "burn_before": dt.datetime.fromisoformat(scar["before"]), "gap_days": 5.0, "scene_pre": scar["pre"],
        "scene_post": scar["post"], "collection": scar["col"], "hazy": False, "dnbr": 0.31, "nbr_post": -0.12,
        "d_b12": 0.05, "confidence": 0.5, "model_version": RULE.version, "fetch_id": None, **changes,
    }


TILE_SCOPE = ("t.mgrs_tile = :tile AND t.scope = :scope", {"tile": KEEP_TILE, "scope": "hotspots"})
_STORED = ("SELECT block, id, unseen, fire_matches, district, village, confidence, d_b12, model_version "
           "FROM burn_cluster WHERE mgrs_tile = :t")


def test_a_rebuilt_tile_keeps_the_rows_that_came_out_the_same() -> None:
    """GitHub's runner rebuilds every tile from all its passes each day. A scar that comes out with
    the same outline and window must keep its id (case links), fire verdict and names: untouched if
    nothing changed, its computed columns updated in place if they did (a new confidence formula or
    rule version). Vanished scars go and new ones arrive named. Rewriting all ~277k scars daily
    filled Neon's 1 GB in November 2026."""
    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    scope, params = TILE_SCOPE
    same, changed, gone = _scar_row(scar, "r0_a"), _scar_row(scar, "r0_b", 2), _scar_row(scar, "r0_c", 4)
    new = _scar_row(scar, "r0_d", 6)
    with SessionLocal() as session:
        first = job._replace_rows(session, BurnCluster, scope, params, [same, changed, gone])
        ids = dict(session.execute(text(
            "SELECT block, id FROM burn_cluster WHERE mgrs_tile = :t"), {"t": KEEP_TILE}).all())
        # verdicts and names set after the first run (fire matching, labels) must survive the rebuild
        session.execute(text("UPDATE burn_cluster SET unseen = true, fire_matches = 0, district = 'D', "
                             "village = 'V' WHERE id = ANY(:i)"), {"i": [ids["r0_a"], ids["r0_b"]]})
        rebuilt = [dict(same), {**changed, "confidence": 0.61, "d_b12": 0.09, "model_version": "test-rules"}, new]
        again = job._replace_rows(session, BurnCluster, scope, params, rebuilt)
        rows = {r.block: r for r in session.execute(text(_STORED), {"t": KEEP_TILE})}
        idle = job._replace_rows(session, BurnCluster, scope, params, [dict(r) for r in rebuilt])
        session.rollback()
    assert first == {"computed": 3, "kept": 0, "updated": 0, "deleted": 0, "inserted": 3, "forwarded": 0}
    # the vanished scar (r0_c) has no successor of its burn: its id is not forwarded
    assert again == {"computed": 3, "kept": 1, "updated": 1, "deleted": 1, "inserted": 1, "forwarded": 0}
    assert set(rows) == {"r0_a", "r0_b", "r0_d"}
    assert rows["r0_a"].id == ids["r0_a"] and rows["r0_a"].unseen is True and rows["r0_a"].fire_matches == 0
    # The same scar with new numbers: same id, verdict and names, the new numbers in place.
    b = rows["r0_b"]
    assert b.id == ids["r0_b"] and b.unseen is True and b.fire_matches == 0 and (b.district, b.village) == ("D", "V")
    assert b.confidence == pytest.approx(0.61) and b.d_b12 == pytest.approx(0.09) and b.model_version == "test-rules"
    assert rows["r0_d"].id not in ids.values() and rows["r0_d"].unseen is None  # new: waits for its verdict
    assert idle == {"computed": 3, "kept": 3, "updated": 0, "deleted": 0, "inserted": 0,
                    "forwarded": 0}  # nothing to write


def test_a_scar_with_another_outline_or_window_is_another_scar() -> None:
    """The verdict and the names belong to an outline and a burn window. If either changes, the old
    row goes and the new one starts without a verdict (fire matching gives it its own)."""
    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    scope, params = TILE_SCOPE
    outline, window = _scar_row(scar, "r0_o"), _scar_row(scar, "r0_w", 2)
    later = window["burn_after"] + dt.timedelta(days=1)
    with SessionLocal() as session:
        job._replace_rows(session, BurnCluster, scope, params, [outline, window])
        before = dict(session.execute(text("SELECT block, id FROM burn_cluster WHERE mgrs_tile = :t"),
                                      {"t": KEEP_TILE}).all())
        session.execute(text("UPDATE burn_cluster SET unseen = true WHERE mgrs_tile = :t"), {"t": KEEP_TILE})
        out = job._replace_rows(session, BurnCluster, scope, params, [
            _scar_row(scar, "r0_o", 0.02), {**window, "burn_after": later}])
        rows = {r.block: r for r in session.execute(text(_STORED), {"t": KEEP_TILE})}
        session.rollback()
    # Both are the same burns (same post pass, overlapping outlines): their old ids are forwarded.
    assert out == {"computed": 2, "kept": 0, "updated": 0, "deleted": 2, "inserted": 2, "forwarded": 2}
    assert all(rows[b].id != before[b] and rows[b].unseen is None for b in ("r0_o", "r0_w"))


def test_updating_a_scar_in_place_writes_one_row_version() -> None:
    """A changed confidence (as after the whole-day gap fix) costs one UPDATE per scar: no delete,
    no insert, no renaming, so a rule change does not reset the table's verdicts or links."""
    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    scope, params = TILE_SCOPE
    rows = [_scar_row(scar, f"r0_u{i}", 2 * i) for i in range(3)]
    writes = text("SELECT n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables WHERE relname = 'burn_cluster'")
    with SessionLocal() as session:
        job._replace_rows(session, BurnCluster, scope, params, rows)
        start = session.execute(writes).one()
        out = job._replace_rows(session, BurnCluster, scope, params, [{**r, "confidence": 0.7} for r in rows])
        end = session.execute(writes).one()
        session.rollback()
    assert out == {"computed": 3, "kept": 0, "updated": 3, "deleted": 0, "inserted": 0, "forwarded": 0}
    assert tuple(e - s for e, s in zip(end, start, strict=True)) == (0, 3, 0)


def test_harvested_fields_still_match_on_their_whole_content() -> None:
    """harvest_cluster has no IDENTITY entry: a field whose computed columns changed (a newer clear
    view) is a new row, as before, and an unchanged one is kept."""
    from shapely import wkt

    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import HarvestCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    poly = wkt.loads(scar["wkt"])
    seen = dt.datetime.fromisoformat(scar["before"])

    def field(block: str, last: dt.datetime) -> dict:
        return {"geom": f"SRID=4326;{poly.wkt}", "centroid": f"SRID=4326;{poly.representative_point().wkt}",
                "area_ha": scar["area"], "n_px": scar["n"], "res_m": 20, "mgrs_tile": KEEP_TILE,
                "scope": block, "harvested_after": seen - dt.timedelta(days=5), "harvested_before": seen,
                "last_seen_unburnt": last, "scene_last": scar["post"], "collection": scar["col"],
                "ndvi_last": 0.2, "model_version": RULE.version, "fetch_id": None}

    scope = ("t.mgrs_tile = :tile AND t.scope = ANY(:scopes)", {"tile": KEEP_TILE, "scopes": ["ha", "hb"]})
    with SessionLocal() as session:
        job._replace_rows(session, HarvestCluster, *scope, [field("ha", seen), field("hb", seen)])
        ids = dict(session.execute(text("SELECT scope, id FROM harvest_cluster WHERE mgrs_tile = :t"),
                                   {"t": KEEP_TILE}).all())
        out = job._replace_rows(session, HarvestCluster, *scope,
                                [field("ha", seen), field("hb", seen + dt.timedelta(days=5))])
        after = dict(session.execute(text("SELECT scope, id FROM harvest_cluster WHERE mgrs_tile = :t"),
                                     {"t": KEEP_TILE}).all())
        session.rollback()
    assert out == {"computed": 2, "kept": 1, "updated": 0, "deleted": 1, "inserted": 1}
    assert after["ha"] == ids["ha"] and after["hb"] != ids["hb"]


def test_a_scope_with_nothing_computed_is_emptied() -> None:
    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    scope, params = TILE_SCOPE
    with SessionLocal() as session:
        job._replace_rows(session, BurnCluster, scope, params, [_scar_row(scar, "r0_e")])
        out = job._replace_rows(session, BurnCluster, scope, params, [])
        left = session.execute(text("SELECT count(*) FROM burn_cluster WHERE mgrs_tile = :t"), {"t": KEEP_TILE}).scalar()
        session.rollback()
    assert out == {"computed": 0, "kept": 0, "updated": 0, "deleted": 1, "inserted": 0, "forwarded": 0}
    assert left == 0


def test_rows_of_a_stage_that_left_a_tile_are_dropped() -> None:
    """A district moved between stages (2026-11-23: seven Punjab districts to the 20 m hotspots
    stage) must not leave the old stage's rows behind: that stage never processes the tile again, so
    they would be counted beside the new stage's rows."""
    from blindspot.db import DeadRows
    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    scar = json.loads((FIXTURES / "matched_scar.json").read_text())["scar"]
    tile = "TEST3"
    with SessionLocal() as session:
        for scope in ("punjab", "hotspots"):
            job._replace_rows(session, BurnCluster, "t.mgrs_tile = :tile AND t.scope = :scope",
                              {"tile": tile, "scope": scope},
                              [_scar_row(scar, f"r0_{scope}", mgrs_tile=tile, scope=scope)])
        session.commit()
    try:
        dead = {name: DeadRows(name) for name in ("burn_cluster", "harvest_cluster")}
        dropped = job.drop_stage_rows("punjab", [tile], dead)
        with SessionLocal() as session:
            left = dict(session.execute(text(
                "SELECT scope, count(*) FROM burn_cluster WHERE mgrs_tile = :t GROUP BY scope"), {"t": tile}).all())
    finally:
        with SessionLocal() as session:
            session.execute(text("DELETE FROM burn_cluster WHERE mgrs_tile = :t"), {"t": tile})
            session.execute(text("DELETE FROM burn_cluster_forward WHERE forwarded_at > now() - interval '1 hour' "
                                 "AND new_id NOT IN (SELECT id FROM burn_cluster)"))
            session.commit()
    assert dropped == {"burn_cluster": 1, "forwarded": 1}  # to the new stage's scar of the same burn
    assert left == {"hotspots": 1}


def test_id_ranges_cover_every_id_once() -> None:
    from blindspot.db import MAX_ID, id_ranges

    with SessionLocal() as session:
        session.execute(text("CREATE TEMP TABLE idr (id bigint) ON COMMIT DROP"))
        session.execute(text("INSERT INTO idr SELECT g FROM generate_series(5, 1000, 7) g"))
        ranges = id_ranges(session, "idr", 30)
        counts = [session.execute(text("SELECT count(*) FROM idr WHERE id >= :lo AND id < :hi"),
                                  {"lo": lo, "hi": hi}).scalar() for lo, hi in ranges]
        session.execute(text("DELETE FROM idr"))
        empty = id_ranges(session, "idr", 30)
        session.rollback()
    assert ranges[0][0] == 0 and ranges[-1][1] == MAX_ID
    assert all(a[1] == b[0] for a, b in pairwise(ranges))  # contiguous, no overlap
    assert sum(counts) == len(range(5, 1001, 7)) and max(counts) == 30
    assert empty == [(0, MAX_ID)]


def test_fire_matching_in_steps_gives_the_same_verdicts() -> None:
    """match_fires_in_batches (one transaction per step, a VACUUM between) sets exactly what one
    match_fires over the whole table would. Commits, so only on the tests' own database; cleaned up."""
    from blindspot.db import engine
    from blindspot.jobs import sentinel as job
    from blindspot.models.sentinel import BurnCluster

    if not (engine.url.database or "").endswith("_test"):
        pytest.skip("commits: only on the *_test database")
    rec = json.loads((FIXTURES / "matched_scar.json").read_text())
    added: list[int] = []
    with SessionLocal() as session:
        if session.execute(text("SELECT to_regclass('public.fire_detection')")).scalar() is None:
            pytest.skip("fire_detection table not created by the fires module in this database")
        cols = [k for k in rec["fires"][0] if k != "in_window"]
        for fire in rec["fires"]:
            fid = session.execute(text(
                f"INSERT INTO fire_detection ({', '.join(cols)}, geom) VALUES "
                f"({', '.join(':' + c for c in cols)}, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) "
                "ON CONFLICT DO NOTHING RETURNING id"), {c: fire[c] for c in cols}).scalar()
            if fid is not None:
                added.append(fid)
        job._replace_rows(session, BurnCluster, None, {}, [_scar_row(rec["scar"], f"r0_s{i}") for i in range(7)])
        session.commit()
    try:
        out = job.match_fires_in_batches(rows=3)
        with SessionLocal() as session:
            got = session.execute(text(
                "SELECT fire_matches, unseen, nearest_fire_m FROM burn_cluster WHERE mgrs_tile = :t"),
                {"t": KEEP_TILE}).all()
            again = job.match_fires(session)  # one pass over everything: nothing is left to change
            session.rollback()
        assert out["steps"] >= 3 and len(got) == 7 and len(set(got)) == 1  # seven copies, one verdict
        assert again["verdicts_changed"] == 0 and again["verdicts_withdrawn_no_coverage"] == 0
    finally:
        with SessionLocal() as session:
            session.execute(text("DELETE FROM burn_cluster WHERE mgrs_tile = :t"), {"t": KEEP_TILE})
            session.execute(text("DELETE FROM fire_detection WHERE id = ANY(:i)"), {"i": added})
            session.commit()
