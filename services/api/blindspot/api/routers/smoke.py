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

Neon egress (docs/neon-budget.md): a cold serverless instance would read a run's whole trails
column (12 MB for a forward run) to answer a 1.9 MB request. So the finished body of each run and
query is kept in response_body (api/cache.stored_response), xz-compressed, keyed by the run id:
a run never changes once written, so its stored body is valid for as long as the code that
rendered it. The smoke job stores the website's variants right after it writes a run (see
jobs/smoke.store_bodies), so even the first visitor reads ~10-15 % of the bytes; any other variant
is derived from the stored full answer (variant_run) and stored by its first request. Receptor summaries are picked out of the summary column in SQL
(the few fields the markers and the headline show, not each city's whole cone and member detail).
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
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response

from blindspot.analytics.trajectory import RECORD_S
from blindspot.api import SessionDep, respond
from blindspot.api.cache import (
    cached_route,
    canonical_key,
    code_version,
    stored_response,
)
from blindspot.models.smoke import latest_id, load_run
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
        if response.status_code != 200:  # a refusal (429) or an error: sent as it is, never kept
            return response
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


# --- stored bodies --------------------------------------------------------------------------------
# The functions below render a run's answer. A stored body's key holds the run id (a run never
# changes once written) and its stamp is the code that renders it.

_RENDERERS = ("blindspot.api.routers.smoke", "blindspot.models.smoke", "blindspot.analytics.trajectory")
Built = tuple[dict[str, Any], list[SourceRef], int]


def body_stamp() -> str:
    return code_version(*_RENDERERS)


def forward_key(run_id: int, max_trails: int | None, stride: int) -> str:
    return canonical_key(f"smoke/forward/{run_id}", max_trails=max_trails, stride=stride)


def back_key(run_id: int, hours: int, asked: int, max_trails: int | None, stride: int) -> str:
    return canonical_key(f"smoke/back/{run_id}", hours=hours, asked=asked if asked > hours else None,
                         max_trails=max_trails, stride=stride)


def validation_key(run_id: int) -> str:
    return f"smoke/validation/{run_id}"


def forward_payload(run: dict[str, Any], max_trails: int | None, stride: int) -> Built:
    """The /forward answer for a loaded run (load_run's dict): payload, sources, max-age."""
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
        payload["reason"] = PRUNED
    if (params.get("inputs") or {}).get("reason"):
        payload["reason"] = params["inputs"]["reason"]
    return payload, _sources(run), 60


def back_payload(run: dict[str, Any], hours: int, asked: int, max_trails: int | None, stride: int) -> Built:
    """The /back answer for a loaded run: payload, sources, max-age."""
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
        payload["reason"] = PRUNED
    if asked > hours:
        payload["hours_note"] = f"asked for {asked} h; back runs cover the last {hours} h"
    return payload, _sources(run), 60


# --- variants from the stored full answer ---------------------------------------------------------
# Any other variant (max_trails, stride, a shorter back window) is a function of the full answer, so
# it is derived from the stored full-resolution body (forward 1.6 MB, a city's back run ~0.2 MB)
# instead of the run's trails column (forward 12-13 MB). The run is rebuilt as load_run returns it,
# so forward_payload / back_payload render the variant by the same code, to the same bytes
# (tests/test_neon_budget.py). Without a stored full answer the run is read once and the full
# answer stored, so the next variant is cheap.

FULL: tuple[int | None, int] = (None, 1)
PRUNED = "trails of this run were pruned"


def _trails_text(p: dict[str, Any], render: Callable[[Any], str]) -> str | None:
    """The trails column as load_run gave it: None for a pruned run (the payload says so), else
    its JSON text by `render` (an empty list stays "[]", which is not pruned)."""
    trails = p.get("trails")
    if trails is None or (not trails and p.get("reason") == PRUNED):
        return None
    return render(trails)


def _run_from_forward(run_id: int, p: dict[str, Any], sources: list[Any]) -> dict[str, Any]:
    # Every forward variant decodes the trails, so any JSON text of the same values will do.
    return {
        "id": run_id, "kind": "forward", "city": None, "run_at": p.get("run_at"), "t0": p.get("t0"),
        "hours": p.get("hours"),
        "params": {"t_end": p.get("t_end"), "inputs": p.get("inputs"), "members": p.get("members"),
                   "method": p.get("method"), "payload": p.get("payload"), "sources": sources},
        "summary": {"notes": p.get("notes")}, "receptors": p.get("receptors"),
        "trails_json": _trails_text(p, lambda t: orjson.dumps(t).decode()),
    }


def _run_from_back(run_id: int, p: dict[str, Any], sources: list[Any], raw: bytes) -> dict[str, Any] | None:
    """A back run rebuilt from its stored full answer (`raw`: that answer's JSON). A 72 h variant
    (`?hours=120` asks for more and gets 72 with a note) passes the trails column's text through
    undecoded, so the text must be Postgres' own: rendered by cache.jsonb_text and accepted only
    when it is exactly the text inside the stored answer (else None: read the run)."""
    import datetime as dt

    from blindspot.api.cache import jsonb_text

    trails_json = _trails_text(p, jsonb_text)
    if trails_json is not None and b'"trails":' + trails_json.encode() + b',' not in raw:
        return None
    return {
        "id": run_id, "kind": "back", "city": p.get("city"),
        "run_at": dt.datetime.fromisoformat(p["run_at"]), "t0": p.get("t0"), "hours": p.get("hours"),
        "params": {"sources": sources},
        "summary": {**(p.get("summary") or {}), "verdict_hint": p.get("verdict_hint"), "notes": p.get("notes")},
        "receptors": [p["receptor"]] if p.get("receptor") is not None else None,
        "trails_json": trails_json,
    }


def variant_run(session: Session, run_id: int, kind: str) -> dict[str, Any] | None:
    """The run as load_run returns it, for rendering a variant: rebuilt from the stored full answer
    when there is one, else read from smoke_run (and its full answer stored)."""
    from blindspot.api.cache import store_payload, stored_payload

    stamp = body_stamp()
    full_key = forward_key(run_id, *FULL) if kind == "forward" else back_key(run_id, 72, 72, *FULL)
    found = stored_payload(session, full_key, stamp)
    if found is not None and found[1] is not None:
        payload, sources_json, raw = found
        sources = orjson.loads(sources_json)
        if payload.get("run_id") == run_id and "trails_selection" not in payload:
            if kind == "forward":
                return _run_from_forward(run_id, payload, sources)
            if payload.get("hours") == 72 and payload.get("run_at"):
                rebuilt = _run_from_back(run_id, payload, sources, raw)
                if rebuilt is not None:
                    return rebuilt
    run = load_run(session, run_id)
    if run is not None:
        full = forward_payload(run, *FULL) if kind == "forward" else back_payload(run, 72, 72, *FULL)
        store_payload(full_key, stamp, *full)
    return run


def validation_payload(run: dict[str, Any]) -> Built:
    """The /validation answer for a loaded run (trails not needed)."""
    summary = run["summary"] or {}
    payload = {"run_at": run["run_at"], "t0": run["t0"], "hours": run["hours"], **summary,
               "series": run["receptors"] or []}
    return payload, _sources(run), 300


# --- routes ---------------------------------------------------------------------------------------


@router.get("/forward")
def forward(request: Request, session: SessionDep, max_trails: MaxTrails = None, stride: Stride = 1):
    """Newest forward run: trails, receptor series, inputs and per-member details."""
    run_id = latest_id(session, "forward")
    if run_id is None:
        return respond({"run_at": None, "t0": None, "hours": None, "step_minutes": int(RECORD_S // 60),
                        "inputs": {"fires": 0, "scars": 0}, "trails": [], "receptors": [],
                        "reason": "no smoke run yet (the smoke job runs after the met job)"}, [], max_age=30)

    def build() -> Response:
        if (max_trails, stride) == FULL:
            return stored_response(session, forward_key(run_id, *FULL), body_stamp(),
                                   lambda: forward_payload(load_run(session, run_id), *FULL))
        return stored_response(
            session, forward_key(run_id, max_trails, stride), body_stamp(),
            lambda: forward_payload(variant_run(session, run_id, "forward"), max_trails, stride), guard=True)

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
        cities = sorted(r["city"] for r in back_summaries(session))
        if cities:
            raise HTTPException(404, f"no back run for {city!r}; cities with back runs: {cities}")
        return respond({"run_at": None, "city": city, "t0": None, "trails": [], "summary": None,
                        "verdict_hint": None, "reason": "no back runs yet (the smoke job runs after met)"},
                       [], max_age=30)

    def build() -> Response:
        if (hours, asked, max_trails, stride) == (72, 72, *FULL):
            run = lambda: load_run(session, run_id)
        else:  # a variant of the city's stored full answer (~0.2 MB), never the trails column again
            run = lambda: variant_run(session, run_id, "back")
        return stored_response(
            session, back_key(run_id, hours, asked, max_trails, stride), body_stamp(),
            lambda: back_payload(run(), hours, asked, max_trails, stride))

    return _cached(request, ("back", run_id, hours, asked if asked > hours else None, max_trails, stride), build)


# The fields of a back run's summary that the receptor markers and the headline read, picked out in
# SQL: a whole summary (cone, per-member detail) is ~12 KB a city.
SUMMARY_FIELDS = ("verdict_hint", "verdict_reason", "share_over_burn_belt", "fire_exposure_index",
                  "fire_exposure_range", "blh_m_now", "wind_from_deg_now")


def back_summaries(session: Session, city: str | None = None) -> list[dict[str, Any]]:
    """The newest back run of every city (or of one, case-insensitive), shaped like the rows of
    models.smoke.latest_back_summaries but with only SUMMARY_FIELDS of the summary, the first
    receptor and the sources of the params."""
    picks = ", ".join(f"summary -> '{k}' AS s_{k}" for k in SUMMARY_FIELDS)
    where = "AND lower(city) = lower(:city)" if city is not None else ""
    latest = f"""
        WITH latest AS (
            SELECT DISTINCT ON (lower(city)) id, city, run_at, summary, receptors, params
            FROM smoke_run WHERE kind = 'back' {where}
            ORDER BY lower(city), run_at DESC, id DESC)"""
    params = {"city": city.strip() if city else None}
    rows = session.execute(text(f"""{latest}
        SELECT id, city, run_at, {picks}, receptors -> 0 AS receptor,
               md5((params -> 'sources')::text) AS sources_md5
        FROM latest ORDER BY lower(city)"""), params).mappings().all()
    # Every back run of one smoke job cites the same sources (~3.5 KB): read each distinct list once.
    sources = dict(session.execute(text(f"""{latest}
        SELECT DISTINCT ON (md5((params -> 'sources')::text)) md5((params -> 'sources')::text), params -> 'sources'
        FROM latest"""), params).all()) if rows else {}
    return [
        {"id": r["id"], "city": r["city"], "run_at": r["run_at"],
         "summary": {k: r[f"s_{k}"] for k in SUMMARY_FIELDS},
         "receptors": [r["receptor"]] if r["receptor"] is not None else None,
         "params": {"sources": sources.get(r["sources_md5"])}}
        for r in rows
    ]


@router.get("/receptors")
@cached_route(ttl=60)
def receptors(session: SessionDep):
    """Every receptor city's newest back-run summary (no trails), for map markers and the agent."""
    rows = back_summaries(session)
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
    return stored_response(session, validation_key(run_id), body_stamp(),
                           lambda: validation_payload(load_run(session, run_id, with_trails=False)))
