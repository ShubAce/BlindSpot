"""Abuse guards on the serverless API: per-client limits counted in Postgres across instances (the
investigator's questions, live picture renders), the client address behind Vercel's proxy, the
featured manifest without background threads, the MGRS tile restriction and bbox snapping.

Pictures are real recorded renders (tests/fixtures/imagery_risk). No test calls an upstream service
or Gemini: the investigation itself is replaced by a stub that only opens and closes the stream (the
limits are decided before it would start). Every client_hit and rendered_asset row a test writes is
deleted by that test, and every test uses its own random documentation-range address, so parallel
runs cannot collide.
"""

from __future__ import annotations

import json
import random
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.agent import runner
from blindspot.db import init_db, session_scope
from blindspot.models import client_quota
from blindspot.sources import pcdata

FX = Path(__file__).parent / "fixtures" / "imagery_risk"
JPG = (FX / "pc_truecolor_good_20261018.jpg").read_bytes()  # a real PC tiler render
GOOD = "S2B_MSIL2A_20261018T053829_R005_T43REQ_20261018T092057"
# The same pass shape on a tile in California (10SEG): a valid id for a scene we do not watch.
ELSEWHERE = "S2B_MSIL2A_20261018T184529_R070_T10SEG_20261018T223311"
# MGRS tiles of every s2_scene row in the dev database on 2026-10-31 (SELECT DISTINCT mgrs_tile).
CATALOGUED_TILES = ["43RCP", "43RDN", "43RDP", "43RDQ", "43REL", "43REM", "43REN", "43REP", "43REQ", "43RFL",
                    "43RFM", "43RFN", "43RFP", "43RFQ", "43RGL", "43RGM", "43RGN", "43RGP", "43RGQ", "43SDR",
                    "43SER", "43SES", "43SFR"]
# Items of the featured frames stored on 2026-10-31 (window near Tarn Taran, 31.55N: tile 43SER).
STORED_FRAME_ITEMS = ["S2C_MSIL2A_20261013T053801_R005_T43SER_20261013T085210",
                      "S2B_MSIL2A_20261018T053829_R005_T43SER_20261018T092057",
                      "S2C_MSIL2A_20261023T053901_R005_T43SER_20261023T084205",
                      "S2B_T43SER_20261028T054808_L2A"]


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


@pytest.fixture
def address() -> Any:
    """A random address from the documentation range 198.51.100.0/24 or 2001:db8::/32; its rows go."""
    if random.random() < 0.5:
        ip = f"198.51.100.{random.randint(1, 254)}"
        key = ip
    else:
        block = f"2001:db8:{random.randint(0, 0xFFFF):x}:{random.randint(0, 0xFFFF):x}"
        ip = f"{block}::{random.randint(1, 0xFFFF):x}"
        key = f"{block}::/64"
    yield ip, key
    client_quota.forget(key)


@pytest.fixture
def vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("SHARED_CLIENT_LIMITS", raising=False)


@pytest.fixture
def client() -> Any:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


# --- the client address ----------------------------------------------------------------------------


def test_forwarded_for_is_trusted_only_on_vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1", "x-real-ip": "203.0.113.9"}
    monkeypatch.delenv("VERCEL", raising=False)
    # Anywhere else any client could send these headers: the socket peer counts.
    assert client_quota.client_address(headers, "192.0.2.10") == "192.0.2.10"
    assert not client_quota.shared_limits_enabled()
    monkeypatch.setenv("VERCEL", "1")
    assert client_quota.shared_limits_enabled()
    assert client_quota.client_address(headers, "127.0.0.1") == "203.0.113.7"  # the first entry
    assert client_quota.client_address({"x-forwarded-for": "not-an-ip", "x-real-ip": "203.0.113.9"},
                                       "127.0.0.1") == "203.0.113.9"
    assert client_quota.client_address({"x-forwarded-for": " ", "x-real-ip": ""}, "127.0.0.1") == "127.0.0.1"
    assert client_quota.client_address({}, None) == "unknown"
    # IPv6 counts per /64 (one subscriber's block); an IPv4-mapped address is the IPv4 address.
    a = client_quota.client_address({"x-forwarded-for": "2001:db8:1:2::1"}, None)
    b = client_quota.client_address({"x-forwarded-for": "2001:db8:1:2:ffff::9"}, None)
    assert a == b == "2001:db8:1:2::/64"
    assert client_quota.client_address({"x-forwarded-for": "::ffff:203.0.113.7"}, None) == "203.0.113.7"
    monkeypatch.setenv("SHARED_CLIENT_LIMITS", "0")
    assert not client_quota.shared_limits_enabled()


def test_only_a_salted_hash_of_the_address_is_stored(vercel: None, address: tuple[str, str]) -> None:
    ip, key = address
    assert client_quota.hit("ask", key, 5, 50, "questions") is None
    with session_scope() as session:
        stored = session.execute(text("SELECT client FROM client_hit WHERE client = :c"),
                                 {"c": client_quota.client_hash(key)}).scalars().all()
    assert len(stored) == 1 and len(stored[0]) == 32
    assert ip not in stored[0] and key not in stored[0]
    # The salt is not a constant anyone can read in the code: a plain SHA-256 of the address differs.
    import hashlib

    assert stored[0] != hashlib.sha256(key.encode()).hexdigest()[:32]


# --- the investigator ------------------------------------------------------------------------------


def _stub_investigation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No Gemini, no tools, no stored row: the stream opens and closes. Returns the run ids."""
    started: list[str] = []

    async def investigate(run: runner.Run, client_factory: Any = None) -> None:
        started.append(run.id)
        run.emit("run", run_id=run.id, model="stub")
        run.emit("done", run_id=run.id, duration_ms=0)

    monkeypatch.setattr(runner, "investigate", investigate)
    monkeypatch.setattr(runner, "save_run", lambda run: None)
    return started


def test_question_limit_holds_across_two_instances(
    vercel: None, address: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two serverless instances (each with its own empty in-memory limiter) share one count."""
    from blindspot.api.main import create_app

    started = _stub_investigation(monkeypatch)
    ip, key = address
    headers = {"x-forwarded-for": f"{ip}, 10.1.2.3"}
    limits = (2, 40)

    def instance() -> TestClient:
        monkeypatch.setattr(runner, "_limiter", runner.RateLimiter(*limits))  # a cold start
        return TestClient(create_app())

    with instance() as a:
        assert a.post("/api/agent/ask", json={"question": f"guard a {uuid.uuid4().hex}"}, headers=headers).status_code == 200
    with instance() as b:
        assert b.post("/api/agent/ask", json={"question": f"guard b {uuid.uuid4().hex}"}, headers=headers).status_code == 200
    with instance() as c:  # a third cold instance: its memory is empty, the database is not
        refused = c.post("/api/agent/ask", json={"question": f"guard c {uuid.uuid4().hex}"}, headers=headers)
        other = c.post("/api/agent/ask", json={"question": f"guard d {uuid.uuid4().hex}"},
                       headers={"x-forwarded-for": "198.51.100.255"})
    client_quota.forget("198.51.100.255")
    assert refused.status_code == 429
    body = refused.json()
    # The API's JSON error shape, and when to come back.
    assert {"detail", "status", "path", "generated_at", "sources"} <= body.keys()
    assert body["status"] == 429 and body["path"] == "/api/agent/ask" and body["sources"] == []
    wait = int(refused.headers["Retry-After"])
    assert 1 <= wait <= 61 and f"Try again in {wait} s" in body["detail"]
    assert refused.headers["Cache-Control"] == "no-store"  # never kept by the CDN
    assert "at most 2 questions a minute" in body["detail"]
    assert other.status_code == 200  # another client is not affected
    assert client_quota.count("ask", key) == 2  # the refused question was not counted
    assert len(started) == 3


def test_replayed_and_joined_questions_are_not_counted(
    vercel: None, address: tuple[str, str], monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    ip, key = address
    _stub_investigation(monkeypatch)
    monkeypatch.setattr(runner, "_limiter", runner.RateLimiter(1, 40))
    stored = {"id": str(uuid.uuid4()), "model": "stub", "started_at": datetime.now(UTC), "duration_ms": 5,
              "events": [{"type": "run", "run_id": "x", "model": "stub"}, {"type": "done", "run_id": "x"}]}
    monkeypatch.setattr(runner, "find_recent", lambda k, minutes: stored)  # answered within 10 min
    for _ in range(4):  # over the limit of 1 a minute, yet every one is a replay
        r = client.post("/api/agent/ask", json={"question": "Why is Delhi's air bad today?"},
                        headers={"x-forwarded-for": ip})
        assert r.status_code == 200 and '"replayed":true' in r.text.replace(" ", "")
    assert client_quota.count("ask", key) == 0
    # A question already running is joined, not counted.
    monkeypatch.setattr(runner, "find_recent", lambda k, minutes: None)
    lang = "en"
    question = f"joined {uuid.uuid4().hex}"
    running = runner.Run(id=str(uuid.uuid4()), kind="ask", question=question, lang=lang,
                         cache_key=runner.cache_key(question, lang))
    running.finish()
    runner._inflight[running.cache_key] = running
    try:
        r = client.post("/api/agent/ask", json={"question": question, "lang": lang}, headers={"x-forwarded-for": ip})
        assert r.status_code == 200
    finally:
        runner._inflight.pop(running.cache_key, None)
    assert client_quota.count("ask", key) == 0


def test_a_broken_counter_does_not_take_the_investigator_down(
    vercel: None, address: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = address

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(client_quota, "hit", boom)
    monkeypatch.setattr(runner, "_limiter", runner.RateLimiter(1, 40))
    runner.count_question(key)  # allowed: memory counted it
    with pytest.raises(runner.RateLimited):
        runner.count_question(key)  # the in-memory first line still holds


# --- featured frames on serverless -------------------------------------------------------------------


def _manifest_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: timedelta) -> dict[str, Any]:
    from blindspot.api.routers import imagery

    payload = orjson.loads((FX / "featured.json").read_bytes())  # built from live data
    payload["built_at"] = (datetime.now(UTC) - age).isoformat()
    path = tmp_path / "featured.json"
    path.write_bytes(orjson.dumps(payload))
    monkeypatch.setattr(imagery, "_featured_path", lambda: path)
    return payload


def test_featured_on_vercel_serves_stale_frames_without_a_thread(
    vercel: None, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery

    monkeypatch.setattr(imagery.threading, "Thread", lambda *a, **k: pytest.fail("no thread on Vercel"))
    _manifest_at(tmp_path, monkeypatch, timedelta(hours=7))  # past the old 6 h, inside the daily job's 30 h
    body = client.get("/api/imagery/featured").json()
    assert body["stale"] is False and body["stale_after_hours"] == 30.0
    assert 419 <= body["age_minutes"] <= 421 and body["frames"]
    _manifest_at(tmp_path, monkeypatch, timedelta(hours=31))
    body = client.get("/api/imagery/featured").json()
    assert body["stale"] is True and 1859 <= body["age_minutes"] <= 1861
    assert len(body["frames"]) == 4  # the stored frames are still served


def test_featured_locally_keeps_the_6_hour_background_refresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery

    monkeypatch.delenv("VERCEL", raising=False)
    started: list[str] = []

    class Recorder:
        def __init__(self, *a: Any, name: str = "", **k: Any) -> None:
            started.append(name)

        def start(self) -> None:
            pass

    monkeypatch.setattr(imagery.threading, "Thread", Recorder)
    _manifest_at(tmp_path, monkeypatch, timedelta(hours=7))
    body = client.get("/api/imagery/featured").json()
    assert body["stale"] is True and body["stale_after_hours"] == 6.0
    assert started == ["featured-refresh"]


def test_featured_on_vercel_never_builds_in_the_request(
    vercel: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blindspot.api.routers import imagery

    monkeypatch.setattr(imagery, "load_featured", lambda: None)  # nothing stored (or no database)
    monkeypatch.setattr(imagery, "build_featured", lambda *a, **k: pytest.fail("no build on Vercel"))
    r = client.get("/api/imagery/featured")
    assert r.status_code == 503 and r.headers["Retry-After"] == "300"
    assert {"detail", "generated_at", "sources"} <= r.json().keys()


# --- the MGRS tiles of the area we watch ------------------------------------------------------------


def test_watched_tiles_are_derived_from_the_aoi_and_cover_the_data() -> None:
    from pyproj import Transformer

    tiles = pcdata.watched_tiles()
    assert set(CATALOGUED_TILES) <= tiles  # every tile the sentinel job has catalogued
    featured = json.loads((FX / "featured.json").read_text())
    for item in [f["item_id"] for f in featured["frames"]] + STORED_FRAME_ITEMS:
        assert pcdata.item_tile(item) in tiles
    assert "10SEG" not in tiles and pcdata.item_tile(ELSEWHERE) == "10SEG"
    # Nothing far away: every tile's zone and band lie next to the AOI (zones 43/44, bands R/S).
    assert {t[:3] for t in tiles} <= {"43R", "43S", "44R", "44S"}
    # The pure-Python UTM agrees with PROJ to well under a metre over the AOI and both zones.
    for zone in (43, 44):
        proj = Transformer.from_crs(4326, 32600 + zone, always_xy=True)
        for lon, lat in [(73.6, 27.4), (75.5, 31.55), (77.9, 32.8), (78.4, 29.0)]:
            x, y = proj.transform(lon, lat)
            ux, uy = pcdata.utm_forward(lon, lat, zone)
            assert abs(x - ux) < 1 and abs(y - uy) < 1
    # 43REQ: the 100 km square at 500-600 km E, 3,400-3,500 km N of zone 43 (column E, row Q).
    assert pcdata.mgrs_tiles((75.2, 31.0, 75.3, 31.1)) == {"43REQ"}


@pytest.mark.parametrize("path", [
    f"/api/imagery/tilejson/truecolor/{ELSEWHERE}",
    f"/api/imagery/tiles/truecolor/{ELSEWHERE}/13/5846/3424.png",
    f"/api/imagery/render?collection=sentinel-2-l2a&item={ELSEWHERE}&bbox=75.2,31.35,75.215,31.36",
])
def test_items_off_the_watched_tiles_are_refused_without_upstream_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(pcdata, "fetch", lambda *a, **k: pytest.fail("no upstream call"))
    monkeypatch.setattr(pcdata, "get_item", lambda *a, **k: pytest.fail("no STAC lookup"))
    monkeypatch.setattr(pcdata, "render", lambda *a, **k: pytest.fail("no render"))
    r = client.get(path)
    assert r.status_code == 400 and "10SEG" in r.json()["detail"]


def test_a_missing_item_is_looked_up_upstream_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from blindspot.http import FetchError

    calls: list[str] = []
    item = f"S2B_MSIL2A_20261018T053829_R005_T43REQ_2026101{random.randint(0, 9)}T{random.randint(100000, 999999)}"

    def fetch(source: str, url: str, **k: Any) -> Any:
        calls.append(url)
        raise FetchError(source, "404 Not Found", status_code=404)

    monkeypatch.setattr(pcdata, "fetch", fetch)
    for _ in range(3):
        with pytest.raises(FetchError):
            pcdata.get_item(pcdata.PC_S2, item)
    assert len(calls) == 1
    pcdata._missing.pop((pcdata.PC_S2, item), None)


# --- render: snapping and the live render limit ------------------------------------------------------


def _params(req: pcdata.RenderRequest, bbox: tuple[float, ...] | None = None) -> dict[str, Any]:
    return {"collection": req.collection, "item": req.item, "kind": req.kind, "w": req.width, "h": req.height,
            "bbox": ",".join(f"{v:.6f}" for v in (bbox or req.bounds or ()))}


def _fake_renderer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[pcdata.RenderRequest]:
    drawn: list[pcdata.RenderRequest] = []

    def render_uncached(req: pcdata.RenderRequest, min_valid: float) -> pcdata.Rendered:
        drawn.append(req)
        return pcdata.Rendered(JPG, "image/jpeg", "pc_data", req.collection, req.item, "recorded", None,
                               datetime(2026, 10, 30, tzinfo=UTC))

    monkeypatch.setattr(pcdata, "_cache", pcdata.ImageCache(tmp_path / "imagery", 10**8))  # a cold instance
    monkeypatch.setattr(pcdata, "_render_uncached", render_uncached)
    return drawn


def test_snapping_is_small_stable_and_scale_aware() -> None:
    from blindspot.api.routers.imagery import _bbox_km2, snap_bbox

    chip = (75.200123, 31.350456, 75.210789, 31.359512)  # about 1 km
    snapped = snap_bbox(chip)
    assert snap_bbox(snapped) == snapped  # on the grid already: unchanged
    assert snap_bbox(tuple(v + 0.000004 for v in chip)) == snapped  # 40 cm of jitter: the same box
    side = chip[2] - chip[0]
    assert all(abs(a - b) <= side / 64 + 1e-6 for a, b in zip(chip, snapped, strict=True))
    assert abs(_bbox_km2(snapped) / _bbox_km2(chip) - 1) < 0.07
    crop = (74.9, 30.9, 75.15, 31.15)  # ~24 km: a coarser grid
    assert abs(snap_bbox(crop)[0] - crop[0]) <= (crop[2] - crop[0]) / 64


def test_stored_frames_and_chips_keep_their_exact_urls_byte_for_byte(
    vercel: None, address: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery
    from blindspot.models.assets import save_asset

    ip, key = address
    drawn = _fake_renderer(monkeypatch, tmp_path)
    manifest = _manifest_at(tmp_path, monkeypatch, timedelta(hours=1))
    frames = imagery._frame_requests(manifest)
    stored_chip = pcdata.RenderRequest(pcdata.PC_S2, GOOD, "swir", 512, 512, "jpg",
                                       bounds=(75.200123, 31.350456, 75.210789, 31.359512))
    assert imagery.snap_bbox(stored_chip.bounds) != stored_chip.bounds  # off the grid on purpose
    stored_bytes = JPG + b"stored"  # distinguishable from anything the fake renderer draws
    try:
        save_asset(stored_chip.cache_key(), "chip", "image/jpeg", stored_bytes, source_key="pc_data",
                   collection=pcdata.PC_S2, item=GOOD, rendered_at=datetime(2026, 10, 30, tzinfo=UTC))
        r = client.get("/api/imagery/render", params=_params(stored_chip), headers={"x-forwarded-for": ip})
        assert r.status_code == 200 and r.content == stored_bytes
        assert r.headers["etag"] == f'"{stored_chip.cache_key()}"'
        # A current featured frame (off the grid too) is drawn for its exact box and stored.
        frame = frames[0]
        assert frame.bounds is not None and imagery.snap_bbox(frame.bounds) != frame.bounds
        r = client.get("/api/imagery/render", params=_params(frame), headers={"x-forwarded-for": ip})
        assert r.status_code == 200 and r.headers["etag"] == f'"{frame.cache_key()}"'
        assert drawn[-1].bounds == frame.bounds
        assert r.headers["x-imagery-bbox"] == ",".join(f"{v:.6f}" for v in frame.bounds)
        assert pcdata.stored_keys([frame.cache_key()]) == {frame.cache_key()}
        assert client_quota.count("render", key) == 1  # that one was a live render; the stored chip was not
    finally:
        with session_scope() as session:
            session.execute(text("DELETE FROM rendered_asset WHERE key = ANY(:k)"),
                            {"k": [stored_chip.cache_key(), frames[0].cache_key()]})


def test_jittered_boxes_share_one_render(
    vercel: None, address: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery

    ip, key = address
    drawn = _fake_renderer(monkeypatch, tmp_path)
    monkeypatch.setattr(imagery, "_persist_role", lambda req: None)  # neither a frame nor a case chip
    req = pcdata.RenderRequest(pcdata.PC_S2, GOOD, "truecolor", 320, 200, "jpg",
                               bounds=(75.200123, 31.350456, 75.215789, 31.360512))
    seen = set()
    for i in range(5):  # a few metres apart each time
        bbox = tuple(v + i * 0.00002 for v in req.bounds)
        r = client.get("/api/imagery/render", params=_params(req, bbox), headers={"x-forwarded-for": ip})
        assert r.status_code == 200 and r.content == JPG
        seen.add((r.headers["etag"], r.headers["x-imagery-bbox"]))
    assert len(drawn) == 1 and len(seen) == 1  # one render, one cache entry
    assert drawn[0].bounds == imagery.snap_bbox(req.bounds)
    assert client_quota.count("render", key) == 1  # only the live render counted


def test_live_renders_are_limited_per_client_across_instances(
    vercel: None, address: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery

    ip, key = address
    drawn = _fake_renderer(monkeypatch, tmp_path)
    monkeypatch.setattr(imagery, "_persist_role", lambda req: None)

    def cold_start() -> None:
        monkeypatch.setattr(imagery, "_render_limiter_instance", runner.RateLimiter(2, 300, noun="new pictures"))

    base = (75.30, 31.40, 75.31, 31.41)
    statuses = []
    for i in range(3):
        cold_start()  # every request on a fresh instance: only the database remembers
        bbox = (base[0] + i * 0.02, base[1], base[2] + i * 0.02, base[3])  # different places: live renders
        r = client.get("/api/imagery/render", params={"collection": pcdata.PC_S2, "item": GOOD, "bbox":
                                                      ",".join(map(str, bbox)), "w": 256, "h": 256},
                       headers={"x-forwarded-for": ip})
        statuses.append(r.status_code)
    assert statuses == [200, 200, 429] and len(drawn) == 2
    body = r.json()
    assert {"detail", "status", "path", "generated_at", "sources"} <= body.keys()
    assert int(r.headers["Retry-After"]) >= 1 and "at most 2 new pictures a minute" in body["detail"]
    assert r.headers["Cache-Control"] == "no-store"
    # A picture already drawn is still served to the same client, uncounted.
    cold_start()
    again = client.get("/api/imagery/render", params={"collection": pcdata.PC_S2, "item": GOOD, "bbox":
                                                      ",".join(map(str, base)), "w": 256, "h": 256},
                       headers={"x-forwarded-for": ip})
    assert again.status_code == 200 and len(drawn) == 2
    assert client_quota.count("render", key) == 2


def test_two_instances_counting_at_once_cannot_overshoot(vercel: None, address: tuple[str, str]) -> None:
    """The advisory lock makes concurrent instances take turns: 8 threads, a limit of 3."""
    _, key = address
    results: list[Any] = []

    def one() -> None:
        results.append(client_quota.hit("render", key, 3, 100, "new pictures"))

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is None for r in results) == 3 and client_quota.count("render", key) == 3


@pytest.mark.parametrize("bbox", ["75.2,31.35,75.2000004,31.3500004", "75.2,31.35,75.20005,31.35005"])
def test_a_box_too_small_to_snap_is_a_400_not_a_500(client: TestClient, bbox: str) -> None:
    r = client.get("/api/imagery/render", params={"collection": pcdata.PC_S2, "item": GOOD, "bbox": bbox})
    assert r.status_code == 400 and "longer side" in r.json()["detail"]


# --- repair: findings of the review on 2026-10-31 ------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_remembered_answers() -> Any:
    """runner remembers stored answers per process; no test may see another's."""
    runner._replay_memo.clear()
    yield
    runner._replay_memo.clear()


def test_rate_limiter_peek_counts_nothing_and_block_holds_until_it_lapses() -> None:
    mem = runner.RateLimiter(2, 10, noun="new pictures")
    mem.peek("a")
    mem.hit("a")
    mem.peek("a")  # one of two used: still allowed, and peeking counted nothing
    mem.hit("a")
    with pytest.raises(runner.RateLimited, match="at most 2 new pictures a minute"):
        mem.peek("a")
    mem.block("b", 30, "at most 3 new pictures a minute")  # the shared count refused b
    for check in (mem.peek, mem.hit):
        with pytest.raises(runner.RateLimited) as exc:
            check("b")
        assert 28 < exc.value.retry_after_s <= 30 and "at most 3" in str(exc.value)
    assert not mem._hits.get("b")  # refused, so never counted
    mem.block("c", -1, "lapsed")
    mem.peek("c")
    mem.hit("c")


@pytest.mark.parametrize("path", [
    f"/api/imagery/tilejson/truecolor/{GOOD}%0A",
    f"/api/imagery/tiles/truecolor/{GOOD}%0A/13/5906/3388.png",
    f"/api/imagery/render?collection=sentinel-2-l2a&item={GOOD}%0A&bbox=75.2,31.35,75.215,31.36",
    ("/api/imagery/render?collection=sentinel-2-c1-l2a&item=S2B_T43REQ_20261028T054808_L2A%0A"
     "&bbox=75.2,31.35,75.215,31.36"),
])
def test_an_item_id_with_a_trailing_newline_is_a_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """`$` also matches before a final newline: such an id reached a STAC URL (httpx.InvalidURL, a 500)
    and a response header (a dropped connection). The id patterns end in \\Z now."""
    for item in (GOOD + "\n", "S2B_T43REQ_20261028T054808_L2A\n"):
        assert pcdata.collection_for(item) is None and pcdata.scene_id(item) is None
        assert pcdata.item_tile(item) is None
    monkeypatch.setattr(pcdata, "get_item", lambda *a, **k: pytest.fail("no STAC lookup"))
    monkeypatch.setattr(pcdata, "render", lambda *a, **k: pytest.fail("no render"))
    assert client.get(path).status_code == 400


def test_a_client_past_its_limit_costs_no_case_chip_query(
    vercel: None, address: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Telling a 512 px box is a case-file chip takes a burn_cluster query; a client already refused
    must not cost one per request (it did: 12 requests, 2 renders, 12 queries)."""
    from blindspot.api.routers import imagery

    ip, key = address
    drawn = _fake_renderer(monkeypatch, tmp_path)
    queries: list[pcdata.RenderRequest] = []

    def is_case_chip(session: Any, req: pcdata.RenderRequest, chip_bbox: Any) -> bool:
        queries.append(req)
        return False

    monkeypatch.setattr(imagery, "_is_case_chip", is_case_chip)

    def cold_start() -> None:
        monkeypatch.setattr(imagery, "_render_limiter_instance", runner.RateLimiter(1, 300, noun="new pictures"))

    def chip(i: int) -> Any:
        bbox = f"{75.30 + i * 0.01:.6f},31.40,{75.306 + i * 0.01:.6f},31.406"
        return client.get("/api/imagery/render", params={"collection": pcdata.PC_S2, "item": GOOD, "bbox": bbox,
                                                         "w": 512, "h": 512}, headers={"x-forwarded-for": ip})

    cold_start()
    statuses = [chip(0).status_code, chip(1).status_code]  # the second: this instance's own count
    cold_start()  # a fresh instance: memory empty, the database refuses (and is remembered)
    statuses += [chip(2).status_code]
    last = chip(3)
    statuses += [last.status_code]
    assert statuses == [200, 429, 429, 429]
    assert len(queries) == 2 and len(drawn) == 1  # the first request, and one per fresh instance
    assert last.headers["Cache-Control"] == "no-store"
    assert "at most 1 new pictures a minute" in last.json()["detail"]
    assert client_quota.count("render", key) == 1


def _with_bounds(req: pcdata.RenderRequest, b: tuple[float, ...]) -> pcdata.RenderRequest:
    from dataclasses import replace

    return replace(req, bounds=(b[0], b[1], b[2], b[3]))


def test_the_case_chip_query_returns_only_the_scar_the_box_is_centred_on() -> None:
    """Three scars of one scene under one chip box: the query returns the one the chip is drawn for,
    not every cluster under the box. Rows live only inside a rolled-back transaction."""
    from sqlalchemy.orm import Session

    from blindspot.api.routers import imagery, scars
    from blindspot.db import engine

    item = f"S2B_MSIL2A_20261018T053829_R005_T43REQ_2026101{random.randint(0, 9)}T{random.randint(100000, 999999)}"
    insert = text("""
        INSERT INTO burn_cluster (geom, centroid, area_ha, n_px, res_m, mgrs_tile, scope, block,
            burn_after, burn_before, gap_days, scene_pre, scene_post, collection, hazy, confidence,
            unseen, model_version)
        VALUES (ST_Buffer(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 0.002),
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 4.0, 10, 20, 'T43REQ', 'test', 'test',
                :after, :before, 5, 'test-pre', :item, 'test', false, 0.9, true, 'test')
        RETURNING ST_AsGeoJSON(geom)::json, ST_Y(centroid)
    """)
    seen: list[int] = []

    class Rows:
        def __init__(self, result: Any) -> None:
            self.result = result

        def all(self) -> list[Any]:
            rows = self.result.all()
            seen.append(len(rows))
            return rows

        def scalar_one(self) -> Any:
            return self.result.scalar_one()

    class Spy:  # the session, counting the rows each query returns
        def __init__(self, session: Session) -> None:
            self.session = session

        def execute(self, *a: Any, **k: Any) -> Rows:
            return Rows(self.session.execute(*a, **k))

    with engine.connect() as conn:
        tx = conn.begin()
        try:
            if not imagery._table_exists(Session(bind=conn), "burn_cluster"):
                pytest.skip("burn_cluster does not exist")
            created = [conn.execute(insert, {"lon": lon, "lat": lat, "item": item,
                                             "after": datetime(2026, 10, 13, tzinfo=UTC),
                                             "before": datetime(2026, 10, 18, tzinfo=UTC)}).one()
                       for lon, lat in ((75.30, 31.40), (75.301, 31.4005), (75.2995, 31.3995))]
            spy: Any = Spy(Session(bind=conn))
            geom, lat = created[0]
            b = imagery._round_bbox(scars._chip_bbox(geom, lat))
            req = pcdata.RenderRequest(pcdata.PC_S2, item, "swir", 512, 512, "jpg", bounds=b)
            assert imagery._is_case_chip(spy, req, scars._chip_bbox) is True
            moved = _with_bounds(req, tuple(v + 0.0005 for v in b))
            assert imagery._is_case_chip(spy, moved, scars._chip_bbox) is False
            wide = _with_bounds(req, (75.27, 31.37, 75.32, 31.42))  # covers all three, centred on none
            assert imagery._is_case_chip(spy, wide, scars._chip_bbox) is False
        finally:
            tx.rollback()
    assert seen == [1, 0, 0]


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    import math

    n = 2**z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def test_live_map_tiles_are_limited_per_client_across_instances(
    vercel: None, address: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/tiles was an uncounted way to render: a client refused on /render kept drawing through it."""
    from shapely.geometry import box, mapping

    from blindspot.api.routers import imagery

    ip, key = address
    drawn = _fake_renderer(monkeypatch, tmp_path)
    lookups: list[str] = []

    def get_item(collection: str, item: str) -> dict[str, Any]:
        lookups.append(item)
        return {"geometry": mapping(box(74.9, 30.6, 76.1, 31.7))}  # around the tiles asked for

    monkeypatch.setattr(pcdata, "get_item", get_item)

    def cold_start() -> None:
        monkeypatch.setattr(imagery, "_tile_limiter_instance", runner.RateLimiter(2, 100, noun="new map tiles"))

    x0, y0 = _tile_xy(75.3, 31.4, 14)

    def tile(i: int) -> Any:
        return client.get(f"/api/imagery/tiles/swir/{GOOD}/14/{x0 + i}/{y0}.png", headers={"x-forwarded-for": ip})

    statuses = []
    for i in range(3):
        cold_start()
        statuses.append(tile(i).status_code)
    refused = tile(3)  # same instance as the refusal: remembered, so no STAC lookup either
    statuses.append(refused.status_code)
    assert statuses == [200, 200, 429, 429] and len(drawn) == 2
    assert len(lookups) == 3  # the 4th request stopped before get_item
    assert refused.headers["Cache-Control"] == "no-store" and int(refused.headers["Retry-After"]) >= 1
    assert "at most 2 new map tiles a minute" in refused.json()["detail"]
    again = tile(0)  # a tile already drawn is served to the refused client, uncounted
    assert again.status_code == 200 and again.content == JPG and len(drawn) == 2
    assert client_quota.count("tiles", key) == 2 and client_quota.count("render", key) == 0


def test_a_stored_answer_is_read_from_the_database_once_per_instance(
    vercel: None, address: tuple[str, str], monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Replays are free for the client, so each must not cost a ~15 KB read of the run's events."""
    ip, key = address
    _stub_investigation(monkeypatch)
    reads: list[str] = []
    stored = {"id": str(uuid.uuid4()), "model": "stub", "started_at": datetime.now(UTC), "duration_ms": 5,
              "events": [{"type": "run", "run_id": "x", "model": "stub"}, {"type": "done", "run_id": "x"}]}

    def find_recent(k: str, minutes: float) -> dict[str, Any]:
        reads.append(k)
        return stored

    monkeypatch.setattr(runner, "find_recent", find_recent)
    question = f"replayed {uuid.uuid4().hex}"
    for _ in range(5):
        r = client.post("/api/agent/ask", json={"question": question}, headers={"x-forwarded-for": ip})
        assert r.status_code == 200 and '"replayed":true' in r.text.replace(" ", "")
    assert len(reads) == 1 and client_quota.count("ask", key) == 0
    # An answer at the end of its replay window is not remembered past it.
    runner._replay_memo.clear()
    stored["started_at"] = datetime.now(UTC) - timedelta(minutes=11)
    for _ in range(2):
        client.post("/api/agent/ask", json={"question": question}, headers={"x-forwarded-for": ip})
    assert len(reads) == 3
