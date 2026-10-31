"""Smoke endpoints, read from smoke_run (the smoke job fills it; routes never run the model).

- GET /api/smoke/forward: the newest forward run: trails (both ensemble members) and each city's
  smoke-index series with its range across the members.
- GET /api/smoke/back?city=Delhi&hours=72: where the city's air came from: trails, the contract
  summary, the two-model cone and the verdict hint.
- GET /api/smoke/receptors: the newest back-run summary of every city in one call (map markers).
- GET /api/smoke/validation: the modelled smoke index against observed PM2.5.

The forward and back payloads run to megabytes, so each is rendered once per stored run, gzipped
once, and kept in a small in-process cache: repeat requests cost a primary-key lookup. Two optional
parameters make them smaller (a serverless response is capped at 4.5 MB): `max_trails` keeps the
heaviest trails of each ensemble member in turn, `stride` keeps every n-th point of each trail.
"""

from __future__ import annotations

import gzip
import threading
from collections.abc import Callable
from itertools import zip_longest
from typing import Annotated, Any

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


MaxTrails = Annotated[int | None, Query(ge=1, le=10_000, description=(
    "keep at most this many trails: the heaviest of each ensemble member in turn (deterministic)"))]
Stride = Annotated[int, Query(ge=1, le=100, description=(
    "keep every n-th point of each trail path and its times (the last point is always kept)"))]


def _select_trails(trails: str | list[Any], max_trails: int | None, stride: int) -> tuple[Any, dict[str, Any] | None]:
    """Trails (stored JSON text, or a list) thinned for a smaller payload, and a note saying how
    (None when nothing was asked: then stored text passes through without decoding).

    `max_trails` takes the heaviest trail of each member (`model`) in turn, ties broken by id, so
    the same run always gives the same trails and both models stay represented; they come back in
    their stored order. `stride` keeps points 0, n, 2n, ... and the last one, so a trail still ends
    where the model put it."""
    if max_trails is None and stride == 1:
        return (orjson.Fragment(trails) if isinstance(trails, str) else trails), None
    if isinstance(trails, str):
        trails = orjson.loads(trails)
    total = len(trails)
    if max_trails is not None and total > max_trails:
        by_model: dict[Any, list[int]] = {}
        for i, trail in enumerate(trails):
            by_model.setdefault(trail.get("model"), []).append(i)
        ranked = [sorted(ids, key=lambda i: (-(trails[i].get("weight") or 0.0), str(trails[i].get("id"))))
                  for _, ids in sorted(by_model.items(), key=lambda kv: str(kv[0]))]
        order = [i for group in zip_longest(*ranked) for i in group if i is not None]
        trails = [trails[i] for i in sorted(order[:max_trails])]
    if stride > 1:
        trails = [_thin(trail, stride) for trail in trails]
    note = {"max_trails": max_trails, "stride": stride, "trails_total": total, "trails_returned": len(trails),
            "rule": "heaviest trails of each ensemble member in turn; every stride-th point plus the last"}
    return trails, note


def _thin(trail: dict[str, Any], stride: int) -> dict[str, Any]:
    n = len(trail.get("times") or trail.get("path") or [])
    keep = list(range(0, n, stride))
    if n and keep[-1] != n - 1:
        keep.append(n - 1)
    out = dict(trail)
    for name in ("path", "times"):
        if isinstance(trail.get(name), list):
            out[name] = [trail[name][k] for k in keep]
    return out


def _trim_back_trails(trails_json: str | None, run_at_s: float, hours: int) -> str | list[Any]:
    """Back trails shortened to the last `hours` before the run (contract parameter `hours`)."""
    if not trails_json:
        return []
    if hours >= 72:
        return trails_json  # the stored text, passed through without decoding
    cutoff = run_at_s - hours * 3600.0
    out = []
    for trail in orjson.loads(trails_json):
        keep = [k for k, t in enumerate(trail["times"]) if t >= cutoff - 1]
        if len(keep) >= 2:
            out.append({**trail, "path": [trail["path"][k] for k in keep],
                        "times": [trail["times"][k] for k in keep]})
    return out


@router.get("/forward")
def forward(request: Request, session: SessionDep, max_trails: MaxTrails = None, stride: Stride = 1):
    """Newest forward run: trails, receptor series, inputs and per-member details."""
    run_id = latest_id(session, "forward")
    if run_id is None:
        return respond({"run_at": None, "t0": None, "hours": None, "step_minutes": int(RECORD_S // 60),
                        "inputs": {"fires": 0, "scars": 0}, "trails": [], "receptors": [],
                        "reason": "no smoke run yet (the smoke job runs after the met job)"}, [], max_age=30)

    def build() -> Response:
        run = load_run(session, run_id)
        params = run["params"] or {}
        trails, selection = _select_trails(run["trails_json"] or [], max_trails, stride)
        payload = {
            "run_at": run["run_at"],
            "t0": run["t0"],
            "hours": run["hours"],
            "t_end": params.get("t_end"),
            "step_minutes": int(RECORD_S // 60),
            "inputs": params.get("inputs"),
            "trails": trails,
            "receptors": run["receptors"] or [],
            "members": params.get("members"),
            "method": params.get("method"),
            "notes": (run["summary"] or {}).get("notes"),
            "payload": params.get("payload"),
            "run_id": run["id"],
        }
        if selection is not None:
            payload["trails_selection"] = selection
        if not run["trails_json"]:
            payload["reason"] = "trails of this run were pruned"
        if (params.get("inputs") or {}).get("reason"):
            payload["reason"] = params["inputs"]["reason"]
        return respond(payload, _sources(run), max_age=60)

    return _cached(request, ("forward", run_id, max_trails, stride), build)


@router.get("/back")
def back(
    request: Request,
    session: SessionDep,
    city: str = "Delhi",
    hours: int = Query(72, ge=1, le=168),
    max_trails: MaxTrails = None,
    stride: Stride = 1,
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
        trails, selection = _select_trails(
            _trim_back_trails(run["trails_json"], run["run_at"].timestamp(), hours), max_trails, stride)
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
        if selection is not None:
            payload["trails_selection"] = selection
        if not run["trails_json"]:
            payload["reason"] = "trails of this run were pruned"
        if asked > hours:
            payload["hours_note"] = f"asked for {asked} h; back runs cover the last {hours} h"
        return respond(payload, _sources(run), max_age=60)

    return _cached(request, ("back", run_id, hours, asked if asked > hours else None, max_trails, stride), build)


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
