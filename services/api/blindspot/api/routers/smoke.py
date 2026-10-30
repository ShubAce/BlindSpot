"""Smoke endpoints, read from smoke_run (the smoke job fills it; routes never run the model).

- GET /api/smoke/forward: the newest forward run: trails (both ensemble members) and each city's
  smoke-index series with its range across the members.
- GET /api/smoke/back?city=Delhi&hours=72: where the city's air came from: trails, the contract
  summary, the two-model cone and the verdict hint.
- GET /api/smoke/receptors: the newest back-run summary of every city in one call (map markers).
- GET /api/smoke/validation: the modelled smoke index against observed PM2.5.

The forward and back payloads run to megabytes, so each is rendered once per stored run, gzipped
once, and kept in a small in-process cache: repeat requests cost a primary-key lookup.
"""

from __future__ import annotations

import gzip
import threading
from collections.abc import Callable
from typing import Any

import orjson
from cachetools import LRUCache
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import Response

from blindspot.analytics.trajectory import RECORD_S
from blindspot.api import SessionDep, respond
from blindspot.api.cache import cached_route
from blindspot.models.smoke import latest_back_summaries, latest_id, load_run
from blindspot.provenance import SourceRef

router = APIRouter(prefix="/api/smoke", tags=["smoke"])

_cache: LRUCache[tuple[Any, ...], tuple[bytes, bytes, dict[str, str]]] = LRUCache(maxsize=48)
_cache_lock = threading.Lock()


def _sources(run: dict[str, Any]) -> list[SourceRef]:
    return [SourceRef(**ref) for ref in ((run.get("params") or {}).get("sources") or [])]


def _cached(request: Request, key: tuple[Any, ...], build: Callable[[], Response]) -> Response:
    """Serve a rendered response from the cache, gzipped when the client accepts it."""
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None:
        response = build()
        body = bytes(response.body)
        headers = {k: v for k, v in response.headers.items() if k.lower() in ("cache-control",)}
        hit = (body, gzip.compress(body, compresslevel=6), headers)
        with _cache_lock:
            _cache[key] = hit
    body, gz, headers = hit
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(gz, media_type="application/json",
                        headers={**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(body, media_type="application/json", headers={**headers, "Vary": "Accept-Encoding"})


def _trim_back_trails(trails_json: str | None, run_at_s: float, hours: int) -> Any:
    """Back trails shortened to the last `hours` before the run (contract parameter `hours`)."""
    if not trails_json:
        return []
    if hours >= 72:
        return orjson.Fragment(trails_json)  # passed through without decoding
    cutoff = run_at_s - hours * 3600.0
    out = []
    for trail in orjson.loads(trails_json):
        keep = [k for k, t in enumerate(trail["times"]) if t >= cutoff - 1]
        if len(keep) >= 2:
            out.append({**trail, "path": [trail["path"][k] for k in keep],
                        "times": [trail["times"][k] for k in keep]})
    return out


@router.get("/forward")
def forward(request: Request, session: SessionDep):
    """Newest forward run: trails, receptor series, inputs and per-member details."""
    run_id = latest_id(session, "forward")
    if run_id is None:
        return respond({"run_at": None, "t0": None, "hours": None, "step_minutes": int(RECORD_S // 60),
                        "inputs": {"fires": 0, "scars": 0}, "trails": [], "receptors": [],
                        "reason": "no smoke run yet (the smoke job runs after the met job)"}, [], max_age=30)

    def build() -> Response:
        run = load_run(session, run_id)
        params = run["params"] or {}
        payload = {
            "run_at": run["run_at"],
            "t0": run["t0"],
            "hours": run["hours"],
            "t_end": params.get("t_end"),
            "step_minutes": int(RECORD_S // 60),
            "inputs": params.get("inputs"),
            "trails": orjson.Fragment(run["trails_json"]) if run["trails_json"] else [],
            "receptors": run["receptors"] or [],
            "members": params.get("members"),
            "method": params.get("method"),
            "notes": (run["summary"] or {}).get("notes"),
            "payload": params.get("payload"),
            "run_id": run["id"],
        }
        if not run["trails_json"]:
            payload["reason"] = "trails of this run were pruned"
        if (params.get("inputs") or {}).get("reason"):
            payload["reason"] = params["inputs"]["reason"]
        return respond(payload, _sources(run), max_age=60)

    return _cached(request, ("forward", run_id), build)


@router.get("/back")
def back(
    request: Request,
    session: SessionDep,
    city: str = "Delhi",
    hours: int = Query(72, ge=1, le=168),
):
    """Newest back run of a city: where its air came from over the last `hours`. Runs cover 72 h,
    so a longer request is answered with 72 h and says so (never an error for the agent)."""
    asked = hours
    hours = min(hours, 72)
    run_id = latest_id(session, "back", city)
    if run_id is None:
        cities = sorted(r["city"] for r in latest_back_summaries(session))
        if cities:
            raise HTTPException(404, f"no back run for {city!r}; cities with back runs: {cities}")
        return respond({"run_at": None, "city": city, "t0": None, "trails": [], "summary": None,
                        "verdict_hint": None, "reason": "no back runs yet (the smoke job runs after met)"},
                       [], max_age=30)

    def build() -> Response:
        run = load_run(session, run_id)
        summary = dict(run["summary"] or {})
        verdict = summary.pop("verdict_hint", None)
        notes = summary.pop("notes", None)
        trails = _trim_back_trails(run["trails_json"], run["run_at"].timestamp(), hours)
        payload = {
            "run_at": run["run_at"],
            "city": run["city"],
            "t0": run["t0"],
            "hours": hours,
            "trails": trails,
            "summary": summary,
            "verdict_hint": verdict,
            "receptor": (run["receptors"] or [None])[0],
            "notes": notes,
            "run_id": run["id"],
        }
        if not run["trails_json"]:
            payload["reason"] = "trails of this run were pruned"
        if asked > hours:
            payload["hours_note"] = f"asked for {asked} h; back runs cover the last {hours} h"
        return respond(payload, _sources(run), max_age=60)

    return _cached(request, ("back", run_id, hours, asked if asked > hours else None), build)


@router.get("/receptors")
@cached_route(ttl=60)
def receptors(session: SessionDep):
    """Every receptor city's newest back-run summary (no trails), for map markers and the agent."""
    rows = latest_back_summaries(session)
    out, refs = [], {}
    for r in rows:
        s = r["summary"] or {}
        rec = (r["receptors"] or [{}])[0]
        out.append({
            "city": r["city"], "lat": rec.get("lat"), "lon": rec.get("lon"),
            "district": rec.get("district"), "state": rec.get("state"), "run_at": r["run_at"],
            "verdict_hint": s.get("verdict_hint"), "verdict_reason": s.get("verdict_reason"),
            "share_over_burn_belt": s.get("share_over_burn_belt"),
            "fire_exposure_index": s.get("fire_exposure_index"),
            "fire_exposure_range": s.get("fire_exposure_range"),
            "blh_m_now": s.get("blh_m_now"), "wind_from_deg_now": s.get("wind_from_deg_now"),
        })
        for ref in _sources(r):
            refs[(ref.key, ref.fetch_id)] = ref
    payload: dict[str, Any] = {"receptors": sorted(out, key=lambda x: x["city"])}
    if not out:
        payload["reason"] = "no back runs yet"
    return respond(payload, list(refs.values()), max_age=60)


@router.get("/validation")
@cached_route(ttl=300)
def validation(session: SessionDep):
    """Newest comparison of the modelled smoke index with observed PM2.5 (honest, possibly weak)."""
    run_id = latest_id(session, "validation")
    if run_id is None:
        return respond({"run_at": None, "cities": [], "reason": "validation has not run yet"}, [], max_age=60)
    run = load_run(session, run_id, with_trails=False)
    summary = run["summary"] or {}
    payload = {"run_at": run["run_at"], "t0": run["t0"], "hours": run["hours"], **summary,
               "series": run["receptors"] or []}
    return respond(payload, _sources(run), max_age=300)
