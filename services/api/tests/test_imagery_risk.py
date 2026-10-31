"""Tests for imagery (sources/pcdata.py, api/routers/imagery.py) and burn risk (analytics/risk.py,
jobs/risk.py, api/routers/risk.py).

Fixtures under tests/fixtures/imagery_risk/ are real recorded responses (2026-10-30): PC tiler renders
and statistics, Earth Search COG pixels, FIRMS points and archive rows, an Open-Meteo forecast and a
featured payload built from live data. No test calls an upstream service, and no test touches rows
it did not create.
"""

from __future__ import annotations

import io
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import orjson
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from blindspot.analytics import risk as model
from blindspot.http import FetchResult
from blindspot.sources import pcdata

FX = Path(__file__).parent / "fixtures" / "imagery_risk"
GOOD = "S2B_MSIL2A_20261018T053829_R005_T43REQ_20261018T092057"
CORRUPT = "S2B_MSIL2A_20261028T053929_R005_T43REQ_20261028T092239"
E84_ID = "S2B_T43REQ_20261028T054808_L2A"


def _fake_result(data, source="pc_data"):
    return FetchResult(data=data, fetch_id=None, status_code=200, latency_ms=1, url_logged="recorded",
                       source_key=source, fetched_at=datetime.now(UTC))


@pytest.fixture
def client():
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


# --- ids and twins -----------------------------------------------------------------------------------


def test_item_ids_are_parsed_and_validated():
    assert pcdata.collection_for(GOOD) == pcdata.PC_S2
    assert pcdata.collection_for(E84_ID) == pcdata.E84_S2
    for bad in ("../../etc/passwd", "S2B_MSIL2A_x", GOOD + "&x=1", ""):
        assert pcdata.collection_for(bad) is None
    sid = pcdata.scene_id(E84_ID)
    assert (sid.platform, sid.tile, sid.day.isoformat()) == ("S2B", "43REQ", "2026-10-28")
    # Earth Search's s2:product_uri carries the PC id (baseline token and .SAFE removed).
    uri = "S2B_MSIL2A_20261018T053829_N0513_R005_T43REQ_20261018T092057.SAFE"
    assert pcdata.pc_id_from_product_uri(uri) == GOOD


def test_mercator_tile_bounds():
    minx, miny, maxx, maxy = pcdata.mercator_tile_bounds(0, 0, 0)
    assert math.isclose(minx, -pcdata.EARTH_HALF) and math.isclose(maxy, pcdata.EARTH_HALF)
    w, s, e, n = pcdata.tile_lonlat_bounds(13, 5807, 3343)
    assert 75.1 < w < e < 75.3 and 31.2 < s < n < 31.5


# --- the plausibility gate and the picture check (real PC responses) ---------------------------------


@pytest.mark.parametrize("name, ok", [("good", True), ("corrupt", False)])
def test_pc_gate_on_recorded_statistics(monkeypatch, name, ok):
    stats = orjson.loads((FX / f"pc_stats_b12_{name}.json").read_bytes())
    monkeypatch.setattr(pcdata, "fetch", lambda *a, **k: _fake_result(stats))
    pcdata._gate_cache.clear()
    gate = pcdata.pc_gate(f"recorded-{name}")
    assert gate.ok is ok
    if ok:
        assert 0.1 < gate.b12_median < 0.2  # 2531 DN -> 0.153
    else:
        assert gate.b12_median < 0  # 128 DN -> -0.087: PC's 2026-10-28 data fault
        assert "plausibility gate" in gate.reason


def test_check_image_tells_real_from_corrupt():
    good = pcdata.check_image((FX / "pc_truecolor_good_20261018.jpg").read_bytes(), min_valid_fraction=0.9)
    bad = pcdata.check_image((FX / "pc_truecolor_corrupt_20261028.jpg").read_bytes(), min_valid_fraction=0.9)
    assert good.ok and good.mean > 50
    assert not bad.ok and "black" in bad.reason


# --- our renderer reproduces PC's colour recipes (same pixels from both catalogues) -------------------


@pytest.mark.parametrize("kind, bands", [("truecolor", ("B04", "B03", "B02")), ("swir", ("B12", "B8A", "B04")),
                                         ("nbr", ("B8A", "B12"))])
def test_local_recipes_match_pc_tiler(kind, bands):
    dn = np.load(FX / "e84_dn_128.npz")
    ours = pcdata.colourise(kind, [dn[b] for b in bands])[..., :3].astype(float)
    pc = np.asarray(Image.open(FX / f"pc_{kind}_128.png").convert("RGB"), dtype=float)
    assert ours.shape == pc.shape
    corr = np.corrcoef(ours.mean(axis=2).ravel(), pc.mean(axis=2).ravel())[0, 1]
    assert corr > 0.95, corr
    assert np.abs(ours - pc).mean() < 12, np.abs(ours - pc).mean()


def test_palette_png_is_lossless_for_a_colour_map():
    dn = np.load(FX / "e84_dn_128.npz")
    rgba = pcdata.colourise("nbr", [dn["B8A"], dn["B12"]])
    rgba[:10, :10, 3] = 0  # some "no data" too
    png = pcdata.encode(rgba, "png", palette=True)
    img = Image.open(io.BytesIO(png))
    assert img.mode == "P"
    back = np.asarray(img.convert("RGBA"))
    assert np.array_equal(back[..., 3] > 0, rgba[..., 3] > 0)
    valid = rgba[..., 3] > 0
    assert np.array_equal(back[valid][:, :3], rgba[valid][:, :3])  # exact, not approximate


# --- GET /api/imagery/render ----------------------------------------------------------------------------

BBOX = "75.200000,31.350000,75.215000,31.360000"


@pytest.mark.parametrize("params, status", [
    ({"collection": "landsat-c2-l2", "item": GOOD, "bbox": BBOX}, 422),
    ({"collection": "sentinel-2-l2a", "item": E84_ID, "bbox": BBOX}, 400),  # id does not match collection
    ({"collection": "sentinel-2-l2a", "item": GOOD, "bbox": "70,20,70.1,20.1"}, 400),  # outside the AOI
    ({"collection": "sentinel-2-l2a", "item": GOOD, "bbox": "74,30,75,31"}, 400),  # 10,000 km2
    ({"collection": "sentinel-2-l2a", "item": GOOD, "bbox": BBOX, "w": 2048, "h": 2048}, 400),  # 4 MP
    ({"collection": "sentinel-2-l2a", "item": GOOD, "bbox": "a,b,c,d"}, 400),
    ({"collection": "sentinel-2-l2a", "item": GOOD, "bbox": BBOX, "kind": "ndvi"}, 422),
])
def test_render_rejects_bad_requests_without_calling_upstream(client, monkeypatch, params, status):
    monkeypatch.setattr(pcdata, "render", lambda *a, **k: pytest.fail("upstream must not be called"))
    r = client.get("/api/imagery/render", params=params)
    assert r.status_code == status
    body = r.json()
    assert "generated_at" in body and body["sources"] == []


def test_render_serves_a_cached_picture_with_immutable_headers(client, monkeypatch, tmp_path):
    cache = pcdata.ImageCache(tmp_path, 10**8)
    monkeypatch.setattr(pcdata, "_cache", cache)
    monkeypatch.setattr(pcdata, "render_pc", lambda *a, **k: pytest.fail("no upstream call on a cache hit"))
    req = pcdata.RenderRequest(pcdata.PC_S2, GOOD, "truecolor", 320, 200, "jpg",
                               bounds=(75.2, 31.35, 75.215, 31.36))
    data = (FX / "pc_truecolor_good_20261018.jpg").read_bytes()
    cache.put(req.cache_key(), pcdata.Rendered(data, "image/jpeg", "pc_data", pcdata.PC_S2, GOOD,
                                               "recorded", 42, datetime(2026, 10, 30, tzinfo=UTC)))
    r = client.get("/api/imagery/render", params={"collection": "sentinel-2-l2a", "item": GOOD, "bbox": BBOX,
                                                  "kind": "truecolor", "w": 320, "h": 200})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == data
    assert r.headers["cache-control"] == "public, max-age=31536000, s-maxage=31536000, immutable"
    assert r.headers["x-imagery-source"] == "pc_data" and r.headers["x-fetch-id"] == "42"


def test_render_falls_back_to_earth_search_when_pc_pixels_fail_the_gate(monkeypatch, tmp_path):
    """The 2026-10-28 case: PC's statistics say noise, so the same pass is drawn from Earth Search."""
    monkeypatch.setattr(pcdata, "_cache", pcdata.ImageCache(tmp_path, 10**8))
    stats = orjson.loads((FX / "pc_stats_b12_corrupt.json").read_bytes())
    monkeypatch.setattr(pcdata, "fetch", lambda *a, **k: _fake_result(stats))
    pcdata._gate_cache.clear()
    good_jpg = (FX / "pc_truecolor_good_20261018.jpg").read_bytes()
    monkeypatch.setattr(pcdata, "find_twin", lambda item: (pcdata.E84_S2, {"id": E84_ID}))
    monkeypatch.setattr(pcdata, "render_pc", lambda *a, **k: pytest.fail("PC must not render a failed item"))
    monkeypatch.setattr(pcdata, "render_local", lambda item, req: pcdata.Rendered(
        good_jpg, "image/jpeg", "s2_cog_e84", pcdata.E84_S2, item["id"], "recorded", 7, datetime.now(UTC)))
    out = pcdata.render(pcdata.RenderRequest(pcdata.PC_S2, CORRUPT, "truecolor", 320, 200, "jpg",
                                             bounds=(75.2, 31.35, 75.215, 31.36)), min_valid_fraction=0.9)
    assert out.source_key == "s2_cog_e84" and out.item == E84_ID
    assert "plausibility gate" in (out.note or "")


def test_render_never_returns_a_bad_picture(monkeypatch, tmp_path):
    monkeypatch.setattr(pcdata, "_cache", pcdata.ImageCache(tmp_path, 10**8))
    monkeypatch.setattr(pcdata, "pc_gate", lambda item: pcdata.Gate(True, 0.15, "ok"))
    black = (FX / "pc_truecolor_corrupt_20261028.jpg").read_bytes()
    monkeypatch.setattr(pcdata, "render_pc", lambda req: pcdata.Rendered(
        black, "image/jpeg", "pc_data", pcdata.PC_S2, req.item, "recorded", None, datetime.now(UTC)))
    monkeypatch.setattr(pcdata, "find_twin", lambda item: None)
    with pytest.raises(pcdata.RenderError, match="black"):
        pcdata.render(pcdata.RenderRequest(pcdata.PC_S2, GOOD, "truecolor", 320, 200, "jpg",
                                           bounds=(75.2, 31.35, 75.215, 31.36)))
    assert not any(tmp_path.rglob("*.img"))  # nothing invalid was cached


def test_tiles_outside_the_scene_cost_no_render(client, monkeypatch):
    footprint = {"type": "Polygon", "coordinates": [[[75.0, 30.6], [76.2, 30.6], [76.2, 31.6], [75.0, 31.6], [75.0, 30.6]]]}
    monkeypatch.setattr(pcdata, "get_item", lambda c, i: {"id": i, "geometry": footprint, "properties": {}})
    monkeypatch.setattr(pcdata, "render", lambda *a, **k: pytest.fail("no render for an empty tile"))
    # z13 tile over Delhi: inside the AOI box, outside this scene
    r = client.get(f"/api/imagery/tiles/truecolor/{GOOD}/13/5846/3424.png")
    assert r.status_code == 404 and r.json()["detail"] == "tile outside the scene"
    assert client.get(f"/api/imagery/tiles/truecolor/{GOOD}/8/0/0").status_code == 422  # below min zoom
    assert client.get("/api/imagery/tiles/truecolor/not-an-item-id-at-all/13/1/1").status_code == 400


def test_scenes_lists_passes_with_tile_templates(client):
    r = client.get("/api/imagery/scenes", params={"lon": 60.0, "lat": -5.0})  # no scene there
    assert r.status_code == 200
    body = r.json()
    assert body["scenes"] == [] and body["reason"] and "generated_at" in body and "sources" in body
    assert client.get("/api/imagery/scenes", params={"lon": 75.2}).status_code == 422  # lat required


# --- featured frames -------------------------------------------------------------------------------------


def _firms_points() -> np.ndarray:
    lines = (FX / "firms_punjab_10d.csv").read_text().splitlines()[1:]
    return np.array([[float(x) for x in ln.split(",")[:2]] + [1.0] for ln in lines])


def test_densest_window_on_real_firms_points():
    from blindspot.api.routers.imagery import densest_window, window_bbox

    pts = _firms_points()
    assert len(pts) > 1000  # real detections in Indian Punjab over 10 days
    lon, lat, value, count = densest_window(pts)
    assert value == count  # weight 1 per detection
    # Brute force over windows centred on every detection: the grid search finds at least as many
    # (within 10%, the price of sliding on a 200 m grid instead of continuously).
    kx = 111.32 * math.cos(math.radians(lat))
    best = 0
    for cx, cy, _ in pts:
        inside = (np.abs(pts[:, 0] - cx) * kx <= 3.2) & (np.abs(pts[:, 1] - cy) * 111.32 <= 2.0)
        best = max(best, int(inside.sum()))
    assert count >= 0.9 * best
    b = window_bbox(lon, lat)
    width_km = (b[2] - b[0]) * kx
    height_km = (b[3] - b[1]) * 111.32
    assert abs(width_km - 6.4) < 0.01 and abs(height_km - 4.0) < 0.01


def test_featured_serves_the_cached_payload_in_contract_shape(client, monkeypatch, tmp_path):
    from blindspot.api.routers import imagery

    path = tmp_path / "featured.json"
    payload = orjson.loads((FX / "featured.json").read_bytes())
    payload["built_at"] = datetime.now(UTC).isoformat()  # fresh, so no background rebuild starts
    path.write_bytes(orjson.dumps(payload))
    monkeypatch.setattr(imagery, "_featured_path", lambda: path)
    r = client.get("/api/imagery/featured")
    assert r.status_code == 200
    body = r.json()
    assert {"generated_at", "sources", "aoi", "frames"} <= body.keys() and body["stale"] is False
    assert {"name", "bbox"} <= body["aoi"].keys() and len(body["aoi"]["bbox"]) == 4
    assert 1 <= len(body["frames"]) <= 4
    dates = [f["date"] for f in body["frames"]]
    assert dates == sorted(dates)
    for f in body["frames"]:
        assert {"date", "item_id", "collection", "cloud_pct", "truecolor_url", "swir_url", "width", "height"} <= f.keys()
        assert (f["width"], f["height"]) == (1600, 1000)
        assert "/api/imagery/render?" in f["truecolor_url"] and "kind=swir" in f["swir_url"]
    assert all({"key", "label", "url", "fetched_at", "fetch_id"} <= s.keys() for s in body["sources"])


# --- risk: the factors and the metrics ---------------------------------------------------------------------


def test_timing_window():
    assert model.timing_factor(0) == 1.0 and model.timing_factor(10) == 1.0
    assert model.timing_factor(20) == pytest.approx(0.5)
    assert model.timing_factor(30) == 0.0 and model.timing_factor(45) == 0.0
    assert model.timing_factor(None) is None
    t = datetime(2026, 10, 30, tzinfo=UTC)
    mid, fewest, most = model.days_since_harvest(t, t - timedelta(days=12), t - timedelta(days=7))
    assert (mid, fewest, most) == (9.5, 7.0, 12.0)


def test_dryness_and_saturation():
    assert model.dryness_factor(0) == 1.0
    assert model.dryness_factor(10) == pytest.approx(math.exp(-1))
    assert model.dryness_factor(None) is None
    # risk-v3: n / (n + typical): 0 stays 0, the typical count gives 0.5, and it never reaches 1
    assert model.relative(0, 5) == 0.0 and model.relative(None, 5) is None
    assert model.relative(5, 5) == 0.5 and model.relative(15, 5) == 0.75
    assert model.relative(10_000, 5) < 1.0
    assert model.typical([0, 0, 3, 5, 9, None]) == 5.0 and model.typical([0, 0]) == 1.0


def test_combine_rescales_missing_factors_and_rain_multiplies():
    score, points = model.combine(1.0, 0.5, None, 1.0)
    assert score == pytest.approx(100 * (0.4 * 1.0 + 0.4 * 0.5) / 0.8)
    assert set(points) == {"timing", "neighbourhood"}
    wet, _ = model.combine(1.0, 0.5, None, model.dryness_factor(10))
    assert wet == pytest.approx(score * math.exp(-1), rel=1e-3)


def test_score_fields_explains_every_number():
    t = datetime(2026, 10, 30, tzinfo=UTC)
    fields = [model.FieldFeatures(1, 5.0, 12, 3, 4.2, 0.0, 1.0, 40),
              model.FieldFeatures(2, 25.0, 0, 0, 0.0, 15.0, 0.0, 0)]
    a, b = model.score_fields(fields, t)
    assert a.score > b.score
    f = a.factors
    assert f["model"] == model.MODEL_VERSION and f["timing"]["value"] == 1.0
    assert f["neighbourhood"]["fires_7d"] == 12 and f["neighbourhood"]["candidate_unseen_burns_7d"] == 3
    assert f["history"]["fires_last_season"] == 40
    assert sum(f["points"].values()) == pytest.approx(a.score, abs=0.05)


def test_metrics_against_a_reference_implementation():
    from sklearn.metrics import roc_auc_score

    rng = random.Random(7)
    labels = [rng.random() < 0.2 for _ in range(400)]
    scores = [round(rng.random() * 10) + (3 if y else 0) for y in labels]  # many ties on purpose
    assert model.roc_auc(scores, [int(y) for y in labels]) == pytest.approx(roc_auc_score(labels, scores))
    assert model.precision_at_k([0.9, 0.8, 0.1, 0.05], [1, 0, 1, 0], 2) == 0.5
    assert model.precision_at_k([1.0], [1], 2) is None
    # ties at the cut are averaged, never broken by list order (which would leak the labels)
    assert model.precision_at_k([1, 1, 1, 1], [1, 1, 0, 0], 2) == 0.5
    assert abs(model.precision_at_k([2, 1, 1, 1], [0, 1, 0, 0], 2) - 1 / 6) < 1e-9
    assert model.neighbourhood_factor(0.5, 0.25) == 0.375 and model.neighbourhood_factor(None, None) is None
    lo, hi = model.wilson_interval(43, 100)  # by hand: centre 0.4326, half-width 0.0953
    assert lo == pytest.approx(0.3373, abs=1e-3) and hi == pytest.approx(0.5279, abs=1e-3)


# --- risk: the job's inputs (recorded real responses) ---------------------------------------------------------


def test_count_within_matches_great_circle_distance():
    from blindspot.jobs.risk import count_within

    pts = _firms_points()[:, :2]
    centre = pts[len(pts) // 2]
    fast = count_within(pts, np.array([centre[0]]), np.array([centre[1]]), 5.0)[0]
    lat1, lon1 = np.radians(centre[1]), np.radians(centre[0])
    lat2, lon2 = np.radians(pts[:, 1]), np.radians(pts[:, 0])
    hav = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    exact = int((2 * 6371.0088 * np.arcsin(np.sqrt(hav)) <= 5.0).sum())
    assert abs(int(fast) - exact) <= max(1, exact // 100)


def test_rain_forecast_sums_the_next_72_hours(monkeypatch):
    from blindspot.jobs import risk as job

    data = orjson.loads((FX / "openmeteo_two_cells.json").read_bytes())
    monkeypatch.setattr(job, "fetch", lambda *a, **k: _fake_result(data, "openmeteo"))
    times = data[0]["hourly"]["time"]
    now = datetime.fromtimestamp(times[30], UTC)  # 30 h into the record: 24 h back and 72 h ahead exist
    cells = [(75.15, 31.35), (75.25, 31.45)]
    rain = job.rain_forecast(cells, now)
    assert rain.reason is None and set(rain.by_cell) == set(cells)
    hourly = data[0]["hourly"]
    nxt = sum(v or 0 for t, v in zip(hourly["time"], hourly["precipitation"], strict=True) if times[30] < t <= times[30] + 72 * 3600)
    assert rain.by_cell[cells[0]][0] == pytest.approx(nxt)


def test_last_season_keeps_vegetation_fires_and_caches(monkeypatch, tmp_path):
    from blindspot.config import get_settings
    from blindspot.jobs import risk as job

    csv = (FX / "firms_snpp_sp_2025-10-27.csv").read_text()
    calls = []

    def fake_fetch(source, url, **kw):
        calls.append(source)
        return _fake_result(csv, source)

    monkeypatch.setattr(job, "fetch", fake_fetch)
    monkeypatch.setattr(job, "SEASON", ((10, 27), (10, 27)))  # one day, so one recorded response
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    if get_settings().firms_map_key is None:
        pytest.skip("FIRMS_MAP_KEY not configured")
    first = job.last_season_fires(datetime(2026, 10, 30, tzinfo=UTC))
    import pandas as pd

    raw = pd.read_csv(io.StringIO(csv))
    keep = raw[(raw["type"] == 0) & (raw["confidence"].astype(str).str[0] != "l")]
    assert first.reason is None and len(first.points) == len(keep) and len(calls) == 1
    again = job.last_season_fires(datetime(2026, 10, 30, tzinfo=UTC))
    assert len(again.points) == len(keep) and len(calls) == 1  # served from disk, no second fetch


def test_backtest_uses_harvest_dates_when_the_scar_table_has_them(client):
    """The timing branch (on once burn_cluster records harvest dates): timing enters the score and a
    timing-only baseline is reported. The pass pair's fields go straight into the backtest's table
    (as _load_pairs would put them); they sit in the Indian Ocean, where no fire or scar of any test
    exists, so only timing separates them."""
    from blindspot.db import session_scope
    from blindspot.jobs import risk as job

    t0 = datetime(2026, 10, 23, 5, 50, tzinfo=UTC)
    t1 = t0 + timedelta(days=5)
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS bt_win, bt"))
        s.execute(text("CREATE TEMP TABLE bt_win (pair int, tile text, scope text, t0 timestamptz, t1 timestamptz) "
                       "ON COMMIT DROP"))
        s.execute(text(f"CREATE TEMP TABLE bt ({job.BT_COLUMNS}{model.SCORE_COLUMNS}) ON COMMIT DROP"))
        s.execute(text("INSERT INTO bt_win VALUES (0, 'TEST', NULL, :t0, :t1)"), {"t0": t0, "t1": t1})
        # burnt: harvested 3-8 days before t0 (mid 5.5); unburnt: 25-30 days before (mid 27.5); one village each
        rows = [(1, 60.0 + i / 1000, 5.5, f"B{i}", 1.0) for i in range(10)]
        rows += [(0, 60.1 + i / 1000, 27.5, f"U{i}", 2.0) for i in range(12)]
        s.execute(text("""INSERT INTO bt (pair, label, fid, g, t0, village, area, days)
                          VALUES (0, :y, :i, ST_SetSRID(ST_MakePoint(:lon, -5.0), 4326)::geography, :t0, :v, :a, :d)"""),
                  [{"y": y, "i": i, "lon": lon, "t0": t0, "v": v, "a": a, "d": d} for i, (y, lon, d, v, a) in enumerate(rows)])
        job.fill_neighbourhood(s, "bt", t0, "p.t0 = :t0", {"t0": t0})
        for stmt in model.score_sql("bt", "pair"):
            s.execute(text(stmt))
        per_pair = job._pair_metrics(s, history_ok=False, timing=True)
        head = job._headline(s, {"pair": 0, "tile": "TEST", "t0": t0, "t1": t1}, history_ok=False, timing=True)
    m = per_pair[0]
    assert m["auc"] == 1.0 and m["baselines_auc"]["timing_only"] == 1.0 and m["baselines_auc"]["history_only"] is None
    assert m["top_k"] == 2 and m["top_hits"] == 2.0  # round(22 / 10) = 2 best-ranked fields, both burnt
    assert sum(b[0] for b in m["bands"]) == 22 and sum(b[1] for b in m["bands"]) == 10
    assert head["burnt"] == 10 and head["unburnt"] == 12 and head["auc"] == 1.0
    assert head["baselines"]["timing_only"]["auc"] == 1.0 and head["factors_used"][0] == "timing"
    assert head["auc_ci95"] == [1.0, 1.0] and head["precision_ci95"]["10"][0] > 0.6
    v = head["villages"]  # villages of burnt fields carry all the risk; bigger (unburnt) ones do not help
    assert v["villages"] == 22 and v["with_a_burn"] == 10 and v["auc"] == 1.0
    assert v["baseline_area_only"]["auc"] == 0.0 and v["precision_at_k"]["10"] == 1.0


# --- GET /api/risk (rows created here, far outside the AOI, and removed afterwards) -------------------------

TEST_BBOX = "59.9,-5.1,60.3,-4.9"  # Indian Ocean: no other test's rows can be here


def test_risk_route_contract_shape(client):
    from blindspot.db import session_scope

    with session_scope() as s:
        if not s.execute(text("SELECT to_regclass('public.harvest_cluster') IS NOT NULL")).scalar_one():
            pytest.skip("harvest_cluster table not created (sentinel models not present)")
        cols = {c for (c,) in s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'harvest_cluster'"))}
    ids: list[int] = []
    t = datetime(2026, 10, 28, 5, 50, tzinfo=UTC)
    try:
        with session_scope() as s:
            for i, (lon, score) in enumerate(((60.0, 81.5), (60.01, 42.0), (60.2, None))):
                values = {
                    "geom": f"SRID=4326;POLYGON(({lon} -5,{lon + 0.002} -5,{lon + 0.002} -4.998,{lon} -4.998,{lon} -5))",
                    "centroid": f"SRID=4326;POINT({lon + 0.001} -4.999)", "area_ha": 4.9 + i, "n_px": 120,
                    "res_m": 20, "mgrs_tile": "TEST", "harvested_after": t - timedelta(days=10),
                    "harvested_before": t - timedelta(days=5), "last_seen_unburnt": t, "scene_last": "test",
                    "collection": "test", "village": "Testpur" if i < 2 else None, "district": "Test district",
                    "risk_score": score,
                    "risk_factors": None if score is None else orjson.dumps(
                        {"model": "test", "as_of": "2026-10-30T00:00:00Z", "timing": {"days_since_harvest": 7.5}}).decode(),
                    "model_version": "test-imagery-risk",
                }
                if "scope" in cols:
                    values["scope"] = "test"
                keys = ", ".join(values)
                vals = ", ".join(f"CAST(:{k} AS jsonb)" if k == "risk_factors" else f":{k}" for k in values)
                ids.append(s.execute(text(f"INSERT INTO harvest_cluster ({keys}) VALUES ({vals}) RETURNING id"),
                                     values).scalar_one())
        r = client.get("/api/risk", params={"bbox": TEST_BBOX, "limit": 10})
        assert r.status_code == 200
        body = r.json()
        assert {"generated_at", "sources", "as_of", "totals", "features", "ranking", "method"} <= body.keys()
        assert body["totals"]["clusters"] == 3 and body["totals"]["scored"] == 2
        feats = body["features"]["features"]
        assert [f["properties"]["risk_score"] for f in feats] == [81.5, 42.0, None]  # highest first
        props = feats[0]["properties"]
        assert {"id", "area_ha", "harvested_after", "harvested_before", "days_since_harvest", "risk_score",
                "risk_factors", "district", "village", "centroid"} <= props.keys()
        assert props["days_since_harvest"] == 7.5 and feats[0]["geometry"]["type"] == "Polygon"
        live = (datetime.now(UTC) - (t - timedelta(days=7.5))).total_seconds() / 86400  # unscored: from dates
        assert feats[2]["properties"]["days_since_harvest"] == pytest.approx(live, abs=0.1)
        rank = body["ranking"]
        assert len(rank) == 1 and rank[0]["village"] == "Testpur" and rank[0]["clusters"] == 2
        assert {"village", "district", "area_ha", "clusters", "risk_score"} <= rank[0].keys()
        assert client.get("/api/risk", params={"bbox": TEST_BBOX, "min_score": 50}).json()["totals"]["clusters"] == 1
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM harvest_cluster WHERE id = ANY(:ids) AND model_version = 'test-imagery-risk'"),
                      {"ids": ids})


def test_risk_route_without_fields_says_why(client):
    r = client.get("/api/risk", params={"bbox": "59.0,-9.0,59.1,-8.9"})
    assert r.status_code == 200
    body = r.json()
    assert body["features"]["features"] == [] and body["ranking"] == [] and body["reason"]


def test_choose_prefers_clean_passes_spread_over_time():
    """30 Oct 2026, Ludhiana window: a 9%-cloud newest pass must lose to a 0% pass two days older,
    and the frames must spread over the period instead of bunching at the start."""
    from types import SimpleNamespace

    from blindspot.api.routers.imagery import Pass, _choose

    def p(day: str, cloud: float) -> Pass:
        return Pass(day, "S2", "sentinel-2-l2a", {"id": day}, None,
                    stats=SimpleNamespace(cloud_fraction=cloud))  # type: ignore[arg-type]

    clear = [p("2026-10-15", 0.036), p("2026-10-18", 0.0), p("2026-10-20", 0.0), p("2026-10-22", 0.0),
             p("2026-10-25", 0.0), p("2026-10-28", 0.0), p("2026-10-30", 0.091)]
    assert [x.day for x in _choose(clear)] == ["2026-10-15", "2026-10-20", "2026-10-25", "2026-10-28"]
    # too few clean passes: the clearest others top the pool up
    few = [p("2026-10-15", 0.08), p("2026-10-18", 0.0), p("2026-10-30", 0.09), p("2026-10-31", 0.2)]
    assert [x.day for x in _choose(few)] == ["2026-10-15", "2026-10-18", "2026-10-30", "2026-10-31"]
