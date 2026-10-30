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
    assert out.get("clusters_matched", 0) >= 1
    assert expected >= 1  # the recorded detections explain this scar
    assert n == expected and unseen is False
    assert nearest == pytest.approx(nearest_py, abs=2.0)


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
    assert RULE.pixel_version == "s2burn-rules-1" and RULE.version != RULE.pixel_version
    # Wilson interval: 11/11 is not "100 % +- 0"; the lower bound is n / (n + z^2).
    lo, hi = burnscar.wilson_interval(11, 11)
    assert hi == 1.0 and lo == pytest.approx(11 / (11 + 1.96**2), abs=1e-3)
    assert burnscar.wilson_interval(0, 0) is None
    audit = burnscar.audit_summary()
    assert audit["current"] is True
    assert 0 < audit["candidate_unseen"]["precision_ci95"][0] <= audit["candidate_unseen"]["precision"] <= 1


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
