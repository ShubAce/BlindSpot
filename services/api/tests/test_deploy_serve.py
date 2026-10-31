"""Serving the API as a serverless function: CDN cache headers, CORS by origin pattern, the rendered
picture store (rendered_asset), smaller smoke payloads and the Punjabi bulletin's script rule.

Every picture, trail and Gemini reply here is a real recorded one (tests/fixtures/imagery_risk,
tests/fixtures/smoke via test_smoke's helpers, tests/fixtures/agent/gemini_bulletin.json). Rows a
test writes are deleted by that test.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from blindspot.api import cache
from blindspot.api.cache import IMMUTABLE, CDNCacheHeaders, cdn_cache_control, cdn_rule
from blindspot.db import init_db, session_scope
from blindspot.sources import pcdata

FIXTURES = Path(__file__).parent / "fixtures"
FX_IMAGERY = FIXTURES / "imagery_risk"
GOOD = "S2B_MSIL2A_20261018T053829_R005_T43REQ_20261018T092057"
JPG = (FX_IMAGERY / "pc_truecolor_good_20261018.jpg").read_bytes()  # a real PC tiler render


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


@pytest.fixture
def client() -> Any:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def _delete_assets(*keys: str) -> None:
    with session_scope() as session:
        session.execute(text("DELETE FROM rendered_asset WHERE key = ANY(:k)"), {"k": list(keys)})


# --- CDN cache headers ---------------------------------------------------------------------------


def test_cdn_rules_follow_the_job_schedule() -> None:
    """Each group's answers expire together, just after its jobs are due to have finished."""
    at = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)  # 14:30 IST, a Monday

    def header(path: str, current: str | None = None, now: datetime = at) -> str | None:
        rule = cdn_rule(path)
        return None if rule is None else cdn_cache_control(rule, current, now)

    tail = "stale-if-error=604800"
    # hourly group (jobs at :17): the 08:17 run is done by 09:30
    assert header("/api/fires", "public, max-age=30") == (
        f"public, max-age=30, s-maxage=1800, stale-while-revalidate=86400, {tail}")
    assert "s-maxage=1800," in header("/api/stats/headline", "public, max-age=60")
    assert "s-maxage=1800," in header("/api/scars/summary", "public, max-age=60")
    # at night the hourly group waits for the 3-hourly runs (21:17 -> 21:30, then 22:30)
    assert "s-maxage=5400," in header("/api/fires", "public, max-age=30", datetime(2026, 11, 2, 20, 0, tzinfo=UTC))
    # met + smoke (03:17, 09:17...): the next hourly point, 10:30 (then 11:30 for a late start)
    assert "s-maxage=5400," in header("/api/smoke/forward", "public, max-age=60")
    assert "s-maxage=5400," in header("/api/met/now", "public, max-age=60")
    # Sentinel-2 group (11:17): 13:30
    assert header("/api/scars", "public, max-age=60") == (
        f"public, max-age=60, s-maxage=16200, stale-while-revalidate=172800, {tail}")
    assert "s-maxage=16200," in header("/api/risk", "public, max-age=60")
    assert "s-maxage=16200," in header("/api/smoke/validation", "public, max-age=300")
    assert "s-maxage=16200," in header("/api/imagery/featured", None)
    assert header("/api/imagery/featured", None).startswith("public, max-age=60,")  # no max-age: 60 s
    # boundaries: every day at 03:30 (weekly data; a deploy shows within a day)
    assert header("/api/geo/boundaries", "public, max-age=3600") == (
        f"public, max-age=300, s-maxage=66600, stale-while-revalidate=604800, {tail}")
    # the bulletin: 02:30, 03:30, 06:30 while it is written and retried (+1 h for a late start)
    assert "s-maxage=63000," in header("/api/bulletin/latest", "public, max-age=300")
    # never less than a minute, never past the group's cap
    assert "s-maxage=60," in header("/api/fires", "public, max-age=30", datetime(2026, 11, 2, 9, 29, 30, tzinfo=UTC))
    assert header("/api/imagery/render") == header(f"/api/imagery/tiles/swir/{GOOD}/12/1/2") == IMMUTABLE
    assert IMMUTABLE == "public, max-age=31536000, s-maxage=31536000, immutable"
    for path in ("/api/health", "/api/agent/ask", "/docs", "/openapi.json"):
        assert header(path, "public, max-age=10") is None, path
    # the Gemini quota: browsers keep it 10 s, the CDN until the hourly point (polls never reach Neon)
    assert header("/api/agent/status", "public, max-age=10") == (
        f"public, max-age=10, s-maxage=1800, stale-while-revalidate=86400, {tail}")
    assert header("/api/fires", "no-store") is None  # a route's own no-store wins
    assert header("/api/fires", "no-cache") is None
    # a route that chose its own shared lifetime keeps it (a headline with a failed block)
    assert header("/api/stats/headline", "public, max-age=30, s-maxage=60") is None


def test_refresh_wakeups_share_the_hourly_clock() -> None:
    """Every expiry point of every group but the bulletin's 02:30 is an HOURLY point, so one page
    view refreshes every expired group in one database wake-up: at most 21 refresh points a day."""
    from blindspot.api.cache import BULLETIN, DAILY, HOURLY, SIX_HOURLY, WEEKLY

    hourly = {(h, HOURLY.minute) for h in HOURLY.hours}
    points = set(hourly)
    for clock in (SIX_HOURLY, DAILY, BULLETIN, WEEKLY):
        mine = {(h, clock.minute) for h in clock.hours}
        points |= mine
        if clock is not BULLETIN:
            assert mine <= hourly, clock.name
    assert points - hourly == {(2, 30)}
    assert len(points) == 21


def test_every_clock_point_follows_its_cron() -> None:
    """The expiry points are consistent with .github/workflows/data-jobs.yml: each comes after a
    scheduled start of the jobs that feed the group."""
    import yaml

    from blindspot.api.cache import BULLETIN, DAILY, HOURLY, SIX_HOURLY

    workflow = yaml.safe_load((Path(__file__).parents[3] / ".github/workflows/data-jobs.yml").read_text())
    crons = [c["cron"] for c in workflow[True]["schedule"]]  # PyYAML reads the key `on` as True

    def starts(cron: str) -> set[int]:
        minute, hours = cron.split()[:2]
        out: set[int] = set()
        for part in hours.split(","):
            lo, _, hi = part.partition("-")
            out |= set(range(int(lo), int(hi or lo) + 1))
        return {h * 60 + int(minute) for h in out}

    text_ = (Path(__file__).parents[3] / ".github/workflows/data-jobs.yml").read_text()
    for cron in crons:  # every slot picks its jobs, and the long-run timeout names a real slot
        assert f'"{cron}"' in text_.split("case ", 1)[1].split("esac", 1)[0], cron
    timeout_slot = text_.split("timeout-minutes:", 1)[1].split("\n", 1)[0]
    assert any(f"'{cron}'" in timeout_slot for cron in crons), timeout_slot
    hourly = starts(crons[0]) | starts(crons[1])
    met = starts(crons[2])
    sentinel = starts(crons[3])
    bulletin = starts(crons[4]) | starts(crons[5])
    for clock, fed_by in ((HOURLY, hourly), (SIX_HOURLY, met), (DAILY, sentinel), (BULLETIN, bulletin)):
        for h in clock.hours:
            point = h * 60 + clock.minute
            before = [s for s in fed_by if 0 < (point - s) % 1440 <= 200]
            assert before, (clock.name, h)


def test_cdn_headers_on_real_routes(client: TestClient) -> None:
    r = client.get("/api/sources", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    assert re.fullmatch(r"public, max-age=\d+, s-maxage=\d+, stale-while-revalidate=86400, stale-if-error=604800",
                        r.headers["cache-control"]), r.headers["cache-control"]
    assert r.headers["vary"].count("Origin") == 1  # the CDN keeps one copy per origin, listed once
    r = client.get("/api/sources")  # no Origin (a server-side fetch): still keyed by origin
    assert "Origin" in r.headers["vary"]
    for path, status in (("/api/health", 200), ("/api/scars/999999999", 404),
                         ("/api/smoke/back?hours=0", 422), ("/api/bulletin/latest?lang=xx", 422)):
        r = client.get(path)
        assert r.status_code == status, path
        assert "s-maxage" not in r.headers.get("cache-control", ""), path
    r = client.get("/api/agent/status")
    assert r.headers["cache-control"].startswith("public, max-age=10, s-maxage="), r.headers["cache-control"]
    r = client.get("/robots.txt")  # crawlers stay off the API host (each request could wake Neon)
    assert r.status_code == 200 and "Disallow: /" in r.text and "max-age=86400" in r.headers["cache-control"]
    assert "s-maxage" not in client.post("/api/agent/ask", json={}).headers.get("cache-control", "")


def test_an_agent_run_in_progress_is_never_cached() -> None:
    bodies = {"running": b'{"run":{"id":"a","finished_at":null,"ok":false}}',
              "done": b'{"run":{"id":"b","finished_at":"2026-10-31T00:00:00Z","ok":true}}'}

    async def run(request: Request) -> Response:
        return Response(bodies[request.path_params["run_id"]], media_type="application/json",
                        headers={"Cache-Control": "public, max-age=60"})

    app = CDNCacheHeaders(Starlette(routes=[Route("/api/agent/runs/{run_id}", run)]))
    with TestClient(app) as c:
        running = c.get("/api/agent/runs/running")
        done = c.get("/api/agent/runs/done")
    assert running.headers["cache-control"] == "no-store" and running.content == bodies["running"]
    assert "s-maxage=" in done.headers["cache-control"] and done.content == bodies["done"]


def test_cors_allows_website_deployments_by_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    from blindspot.api import main as api_main
    from blindspot.config import get_settings

    monkeypatch.setenv("CORS_ORIGIN_REGEX", r"^https://blindspot[a-z0-9-]*\.vercel\.app$")
    get_settings.cache_clear()
    try:
        app = api_main.create_app()
        with TestClient(app) as c:
            for origin, allowed in (("https://blindspot-git-main-team.vercel.app", True),
                                    ("https://blindspot.vercel.app", True),
                                    ("http://localhost:3000", True),
                                    ("https://evil.vercel.app", False),
                                    ("https://blindspot.vercel.app.evil.com", False)):
                r = c.get("/api/health", headers={"Origin": origin})
                assert (r.headers.get("access-control-allow-origin") == origin) is allowed, origin
    finally:
        monkeypatch.delenv("CORS_ORIGIN_REGEX")
        get_settings.cache_clear()


# --- rendered_asset: the picture store -------------------------------------------------------------


def _render_params(req: pcdata.RenderRequest) -> dict[str, Any]:
    assert req.bounds is not None
    return {"collection": req.collection, "item": req.item, "kind": req.kind, "w": req.width,
            "h": req.height, "bbox": ",".join(f"{v:.6f}" for v in req.bounds)}


IMAGE_HEADERS = ("etag", "cache-control", "x-imagery-source", "x-imagery-collection", "x-imagery-item",
                 "x-fetch-id", "x-rendered-at", "x-imagery-note")


def test_a_stored_picture_is_served_with_the_headers_it_was_rendered_with(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery

    disk = pcdata.ImageCache(tmp_path, 10**8)  # a fresh serverless instance: nothing on disk
    monkeypatch.setattr(pcdata, "_cache", disk)
    monkeypatch.setattr(pcdata, "_render_uncached", lambda *a, **k: pytest.fail("no render: it is stored"))
    req = pcdata.RenderRequest(pcdata.PC_S2, GOOD, "truecolor", 320, 200, "jpg", bounds=(75.2, 31.35, 75.215, 31.36))
    key = req.cache_key()
    rendered = pcdata.Rendered(JPG, "image/jpeg", "s2_cog_e84", pcdata.E84_S2, "S2B_T43REQ_20261018T054808_L2A",
                               "recorded", None, datetime(2026, 10, 30, 5, 40, 12, 345678, tzinfo=UTC),
                               note="PC could not render it; drawn from the same pass on Earth Search")
    expected = imagery._image_response(rendered, key).headers  # what the first render sent
    try:
        assert pcdata.store(key, rendered, "chip")
        first = client.get("/api/imagery/render", params=_render_params(req))
        assert first.status_code == 200 and first.content == JPG
        assert {h: first.headers.get(h) for h in IMAGE_HEADERS} == {h: expected.get(h) for h in IMAGE_HEADERS}
        assert disk.get(key) is not None  # copied to this instance's disk on the way
        again = client.get("/api/imagery/render", params=_render_params(req))
        assert {h: again.headers.get(h) for h in IMAGE_HEADERS} == {h: expected.get(h) for h in IMAGE_HEADERS}
    finally:
        _delete_assets(key)


def test_map_tiles_never_touch_the_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from blindspot.models import assets

    monkeypatch.setattr(pcdata, "_cache", pcdata.ImageCache(tmp_path, 10**8))
    monkeypatch.setattr(assets, "load_asset", lambda key: pytest.fail("a tile was looked up"))
    monkeypatch.setattr(assets, "save_asset", lambda *a, **k: pytest.fail("a tile was stored"))
    png = (FX_IMAGERY / "pc_truecolor_128.png").read_bytes()
    req = pcdata.RenderRequest(pcdata.PC_S2, GOOD, "truecolor", 256, 256, "png", tile=(13, 5846, 3424))
    monkeypatch.setattr(pcdata, "_render_uncached", lambda r, m: pcdata.Rendered(
        png, "image/png", "pc_data", pcdata.PC_S2, GOOD, "recorded", None, datetime.now(UTC)))
    assert pcdata.render(req).data == png
    assert pcdata.render(req).cached  # the second one from disk


def test_featured_job_stores_frames_and_prunes_what_nothing_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blindspot.api.routers import imagery
    from blindspot.models.assets import save_asset

    disk = pcdata.ImageCache(tmp_path / "imagery", 10**8)
    monkeypatch.setattr(pcdata, "_cache", disk)
    monkeypatch.setattr(pcdata, "_render_uncached", lambda *a, **k: pytest.fail("frames are on disk already"))
    manifest = orjson.loads((FX_IMAGERY / "featured.json").read_bytes())  # built from live data
    manifest["built_at"] = datetime.now(UTC).isoformat()
    path = tmp_path / "featured.json"
    path.write_bytes(orjson.dumps(manifest))
    monkeypatch.setattr(imagery, "_featured_path", lambda: path)
    monkeypatch.setattr(imagery, "case_chip_requests", lambda session, limit=24: [])  # scars: another module's
    frames = imagery._frame_requests(manifest)
    assert len(frames) == 2 * len(manifest["frames"]) >= 2
    for req in frames:
        disk.put(req.cache_key(), pcdata.Rendered(JPG, "image/jpeg", "pc_data", req.collection, req.item,
                                                  "recorded", None, datetime(2026, 10, 30, tzinfo=UTC)))
    old, recent = f"test-old-{uuid.uuid4().hex[:8]}", f"test-recent-{uuid.uuid4().hex[:8]}"
    keys = [r.cache_key() for r in frames]
    try:
        for key in (old, recent):
            save_asset(key, "chip", "image/jpeg", JPG)
        with session_scope() as session:
            session.execute(text("UPDATE rendered_asset SET last_used_at = now() - interval '8 days' "
                                 "WHERE key = :k"), {"k": old})
        summary = imagery.store_featured_assets()
        assert summary["frames"] == len(frames) and summary["chips"] == 0
        assert summary["missing"] == 0 and summary["manifest_stored"]
        assert summary["pruned"]["removed"] >= 1
        stored = pcdata.stored_keys([*keys, imagery.FEATURED_KEY, old, recent])
        assert stored == {*keys, imagery.FEATURED_KEY, recent}  # the old unreferenced one is gone
        assert imagery.store_featured_assets()["stored_now"] == 0  # a second run has nothing to do

        # GET /render writes a fresh render through only for a current frame, never for any crop
        assert imagery._persist_role(frames[0]) == "frame"
        assert frames[0].bounds is not None
        moved = pcdata.RenderRequest(frames[0].collection, frames[0].item, "truecolor", 1600, 1000, "jpg",
                                     bounds=tuple(v + 0.01 for v in frames[0].bounds))
        assert imagery._persist_role(moved) is None

        # a serverless instance with an empty disk reads the manifest from the database
        path.unlink()
        assert imagery.load_featured()["aoi"] == manifest["aoi"]
    finally:
        _delete_assets(*keys, imagery.FEATURED_KEY, old, recent)


def test_featured_frame_urls_point_at_this_deployment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manifest built on a job runner or a laptop carries http://localhost:8000 picture URLs."""
    from blindspot.api.routers import imagery
    from blindspot.config import get_settings

    manifest = orjson.loads((FX_IMAGERY / "featured.json").read_bytes())  # built on the dev machine
    manifest["built_at"] = datetime.now(UTC).isoformat()
    assert manifest["frames"][0]["truecolor_url"].startswith("http://localhost:8000/api/imagery/render?")
    path = tmp_path / "featured.json"
    path.write_bytes(orjson.dumps(manifest))
    monkeypatch.setattr(imagery, "_featured_path", lambda: path)
    monkeypatch.setattr(get_settings(), "public_base_url", "https://blindspot-api-azure.vercel.app")
    frames = client.get("/api/imagery/featured").json()["frames"]
    assert len(frames) == len(manifest["frames"])
    for got, stored in zip(frames, manifest["frames"], strict=True):
        for key in ("truecolor_url", "swir_url"):
            assert got[key] == "https://blindspot-api-azure.vercel.app" + stored[key].removeprefix("http://localhost:8000")
        assert {k: v for k, v in got.items() if not k.endswith("_url")} == \
            {k: v for k, v in stored.items() if not k.endswith("_url")}


def test_chip_prewarm_skips_chips_the_store_already_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a runner with an empty disk, a stored chip must not be read back from the database."""
    from contextlib import contextmanager

    from blindspot.jobs import risk as job

    case = orjson.loads((FIXTURES / "agent" / "endpoints_scars_risk.json").read_bytes())["/api/scars/3486"]["case"]
    lat = case["centroid"][1]
    row = (case["geometry"], lat, case["scene_pre"], case["scene_post"], case["collection"])  # a live scar

    class Rows:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one(self) -> Any:
            return self.value

        def all(self) -> list[Any]:
            return self.value

    class Session:
        def execute(self, stmt: Any, params: Any = None) -> Rows:
            return Rows(True) if "to_regclass" in str(stmt) else Rows([row])

    @contextmanager
    def fake_scope() -> Any:
        yield Session()

    monkeypatch.setattr(job, "session_scope", fake_scope)
    asked: list[list[str]] = []
    rendered: list[str] = []

    def render(req: pcdata.RenderRequest, **kwargs: Any) -> Any:
        rendered.append(req.cache_key())
        return pcdata.Rendered(JPG, "image/jpeg", "pc_data", req.collection, req.item, "recorded", None,
                               datetime(2026, 10, 30, tzinfo=UTC))

    monkeypatch.setattr(pcdata, "render", render)
    monkeypatch.setattr(pcdata, "stored_keys", lambda keys: asked.append(list(keys)) or set(keys))
    summary = job.prewarm_case_chips()
    assert summary["chips"] == summary["stored"] == 2 * len(pcdata.KINDS) and rendered == []

    monkeypatch.setattr(pcdata, "stored_keys", lambda keys: set(list(keys)[:2]))
    summary = job.prewarm_case_chips()
    assert summary["stored"] == 2 and len(rendered) == summary["chips"] - 2
    assert set(rendered).isdisjoint(asked[0][:2])


# --- smoke: max_trails and stride --------------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded_forward() -> dict[str, Any]:
    from test_smoke import recorded_fires, recorded_members, recorded_receptors

    from blindspot.jobs import smoke as job

    fires = recorded_fires()
    run_at = datetime.fromtimestamp(fires.t.max(), UTC)
    return job.forward_run(recorded_members(), recorded_receptors(), fires, None, None, run_at, seed=7)


def test_trails_are_thinned_deterministically(recorded_forward: dict[str, Any]) -> None:
    from blindspot.api.routers import smoke

    trails = recorded_forward["trails"]
    stored = orjson.dumps(trails).decode()
    same, note = smoke._select_trails(stored, None, 1)
    assert note is None and isinstance(same, orjson.Fragment)  # the default passes text through
    picked, note = smoke._select_trails(stored, 300, 3)
    assert note is not None and note["trails_total"] == len(trails) and note["trails_returned"] == 300
    assert smoke._select_trails(stored, 300, 3)[0] == picked  # the same run gives the same trails
    ids = [t["id"] for t in trails]
    assert [t["id"] for t in picked] == sorted((t["id"] for t in picked), key=ids.index)  # stored order
    by_model: dict[str, list[dict[str, Any]]] = {}
    for t in picked:
        by_model.setdefault(t["model"], []).append(t)
    assert set(by_model) == {"best_match", "gfs_seamless"}
    assert abs(len(by_model["best_match"]) - len(by_model["gfs_seamless"])) <= 1  # both members
    kept = {t["id"] for t in picked}
    for model, chosen in by_model.items():  # the heaviest of each member
        dropped = [t["weight"] for t in trails if t["model"] == model and t["id"] not in kept]
        assert not dropped or min(t["weight"] for t in chosen) >= max(dropped)
    original = {t["id"]: t for t in trails}
    for t in picked:
        full = original[t["id"]]
        n = len(full["times"])
        assert t["times"][0] == full["times"][0] and t["times"][-1] == full["times"][-1]
        assert t["path"][-1] == full["path"][-1] and len(t["path"]) == len(t["times"])
        assert len(t["times"]) == len(range(0, n, 3)) + (0 if (n - 1) % 3 == 0 else 1)


def test_smoke_forward_route_takes_max_trails_and_stride(
    client: TestClient, recorded_forward: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import delete

    from blindspot.api.routers import smoke as smoke_router
    from blindspot.jobs import smoke as job
    from blindspot.models.smoke import SmokeRun

    monkeypatch.setattr(job, "prune", dict)  # never touch rows other tests own
    smoke_router._cache.clear()
    common = {"met_run_ids": [], "notes": ["test: recorded inputs"],
              "params": {"method": job.method_params(), "sources": [], "members": ["best_match", "gfs_seamless"]}}
    ids = job.store(recorded_forward, [], common, datetime.now(UTC))
    try:
        full = client.get("/api/smoke/forward").json()
        assert "trails_selection" not in full and len(full["trails"]) == len(recorded_forward["trails"])
        r = client.get("/api/smoke/forward", params={"max_trails": 200, "stride": 2})
        assert r.status_code == 200
        small = r.json()
        assert len(small["trails"]) == 200 and small["trails_selection"]["stride"] == 2
        assert set(small) - {"trails_selection"} == set(full)  # additive only
        assert client.get("/api/smoke/forward", params={"stride": 0}).status_code == 422
        assert client.get("/api/smoke/forward", params={"max_trails": 0}).status_code == 422
    finally:
        with session_scope() as session:
            session.execute(delete(SmokeRun).where(SmokeRun.id == ids["forward"]))
        smoke_router._cache.clear()


# --- the Punjabi bulletin ------------------------------------------------------------------------------

BULLETIN = orjson.loads((FIXTURES / "agent" / "gemini_bulletin.json").read_bytes())
# The recorded real session of 2026-10-30: calls[1] wrote the English, calls[4] the Hindi and
# calls[5] the Punjabi translation. The Hindi reply, offered as Punjabi, is a real reply in the
# wrong script: exactly what the script check must catch.
EN_CALL, HI_CALL, PA_CALL = BULLETIN["calls"][1], BULLETIN["calls"][4], BULLETIN["calls"][5]
NOW = datetime(2026, 10, 30, 18, 28, tzinfo=UTC)


class Replay:
    """Duck-types google-genai's async client: one recorded call per request, in order."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = list(calls)
        self.requests: list[dict[str, Any]] = []
        self.aio = self
        self.models = self

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
        self.requests.append({"model": model, "config": config, "turn": contents[0].parts[0].text})
        chunks = [types.GenerateContentResponse.model_validate_json(c) for c in self.calls.pop(0)["chunks"]]

        async def stream() -> Any:
            for chunk in chunks:
                yield chunk

        return stream()

    async def aclose(self) -> None:
        return None

    def close(self) -> None:
        return None


def _book_and_english() -> tuple[Any, dict[str, Any]]:
    from blindspot.agent import bulletin
    from blindspot.agent.tools import CitationBook
    from blindspot.provenance import SourceRef

    book = CitationBook()
    for _, ref in sorted(BULLETIN["refs"].items(), key=lambda kv: int(kv[0])):
        book.mark(SourceRef.model_validate(ref))
    raw = "".join(part.text for c in EN_CALL["chunks"]
                  for cand in (types.GenerateContentResponse.model_validate_json(c).candidates or [])[:1]
                  for part in ((cand.content.parts if cand.content else None) or [])
                  if part.text and not part.thought)  # fmt: skip
    checker = bulletin.Checker.for_facts(BULLETIN["facts"], book, NOW)
    return book, bulletin.publish("en", bulletin.parse(raw), checker, book, "recorded", NOW)


def _cleanup_run(run_id: str) -> None:
    with session_scope() as session:
        session.execute(text("DELETE FROM bulletin WHERE run_id = :id"), {"id": run_id})
        session.execute(text("DELETE FROM agent_run WHERE id = :id"), {"id": run_id})
        session.execute(text("DELETE FROM source_fetch WHERE source_key = 'gemini' AND meta->>'run_id' = :id"),
                        {"id": run_id})


def _translate(calls: list[dict[str, Any]]) -> tuple[dict[str, Any], Replay]:
    from blindspot.agent import bulletin, runner

    runner.reset_gateway_state()
    book, english = _book_and_english()
    client = Replay(calls)
    run_id = str(uuid.uuid4())
    gateway = runner.Gateway("bulletin", run_id, client_factory=lambda: client)
    try:
        checker = bulletin.Checker.for_translation(english, book, NOW)
        out = asyncio.run(bulletin.translate("pa", english, checker, book, gateway, NOW))
    finally:
        _cleanup_run(run_id)
    return out, client


def test_punjabi_is_asked_for_gurmukhi_only_and_retried_on_the_other_model() -> None:
    out, client = _translate([HI_CALL, PA_CALL])
    first, second = client.requests
    assert first["turn"].startswith("Translate into Punjabi")
    assert "U+0A00 to U+0A7F" in first["config"].system_instruction
    assert first["config"].temperature == 0.2
    assert second["model"] != first["model"]  # the other model of the bulletin chain
    assert out["verification"]["script_ok"] and out["verification"]["passed"]
    assert out["model"] == second["model"]


def test_punjabi_that_fails_twice_is_stored_flagged_and_reported_unavailable(client: TestClient) -> None:
    from blindspot.agent import bulletin
    from blindspot.models.agent import AgentRun

    out, replay = _translate([HI_CALL, HI_CALL])
    assert len(replay.requests) == 2  # one retry, no more
    assert not out["verification"]["script_ok"] and out["verification"]["foreign_script_words"]
    run_id, day = str(uuid.uuid4()), date(2099, 12, 31)  # newer than any real bulletin
    try:
        with session_scope() as session:
            session.add(AgentRun(id=run_id, kind="bulletin", question="script test", lang="en",
                                 started_at=datetime.now(UTC)))
        bulletin._store(day, {"pa": out}, run_id)
        cache.clear()
        body = client.get("/api/bulletin/latest", params={"lang": "pa"}).json()
        assert body["date"] == "2099-12-31" and body["available"] is False
        assert "unavailable" in body["reason"] and "script" in body["reason"]
        assert body["verification"]["script_ok"] is False  # the shape is unchanged
        en = client.get("/api/bulletin/latest", params={"lang": "en"})
        assert en.status_code == 404 or en.json()["available"] is True
    finally:
        _cleanup_run(run_id)
        cache.clear()


def test_a_later_run_translates_only_the_missing_language_from_the_stored_english() -> None:
    from blindspot.agent import bulletin, runner
    from blindspot.models.agent import AgentRun

    runner.reset_gateway_state()
    _, english = _book_and_english()
    run_id, day = str(uuid.uuid4()), date(2099, 12, 30)
    now = datetime(2099, 12, 30, 3, 0, tzinfo=UTC)  # 08:30 IST on that day
    client = Replay([PA_CALL])
    try:
        with session_scope() as session:
            session.add(AgentRun(id=run_id, kind="bulletin", question="partial test", lang="en",
                                 started_at=datetime.now(UTC)))
        bulletin._store(day, {"en": english, "hi": {**english, "verification": {"script_ok": True}}}, run_id)
        summary = asyncio.run(bulletin.write_bulletin(now=now, client_factory=lambda: client))
        assert summary["translated_from"] == "stored English" and set(summary["languages"]) == {"pa"}
        assert summary["languages"]["pa"]["script_ok"] and summary["gemini_calls"] == 1
        assert len(client.requests) == 1  # no English call, no Hindi call
        assert bulletin._written(day) == {"en", "hi", "pa"}
    finally:
        with session_scope() as session:
            new = session.execute(text("SELECT DISTINCT run_id FROM bulletin WHERE date = :d"),
                                  {"d": day}).scalars().all()
        for rid in {run_id, *[r for r in new if r]}:
            _cleanup_run(rid)
