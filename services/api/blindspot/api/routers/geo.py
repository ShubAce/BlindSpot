"""Administrative areas and places, read from the boundary and place tables (job `boundaries`).

    GET /api/geo/boundaries   state / district / tehsil polygons as GeoJSON, simplified for the web
    GET /api/geo/districts    the district list without geometry: LGD codes, aliases, area
    GET /api/geo/cities       receptor cities, with population and where it comes from
    GET /api/geo/lookup       the state, district and tehsil of a point, and the nearest village
    GET /api/geo/places       find a village, town or city by name
    GET /api/geo/tiles/{z}/{x}/{y}.pbf   the same polygons as vector tiles, for drawing state and
                              district lines at every zoom (states are Survey of India's)

Simplified polygons come from coverage simplification: the shared border of two districts is
simplified once, so neighbours never gap or overlap on the map, however far one zooms in.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Sequence
from typing import Annotated, Any, Literal

import numpy as np
import shapely
from cachetools import LRUCache, TTLCache
from fastapi import APIRouter, HTTPException, Path, Query
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response

from blindspot.api import BBoxDep, SessionDep, respond
from blindspot.api.cache import (
    cached_route,
    canonical_key,
    code_version,
    fetch_binary,
    stored_response,
    table_stamp,
)
from blindspot.geo import district_for_point, nearest_place
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch
from blindspot.sources import boundaries as _sources  # noqa: F401  (registers source labels)

router = APIRouter(prefix="/api/geo", tags=["geo"])

Level = Literal["state", "district", "subdistrict"]

ATTRIBUTION = (
    "State boundaries: Survey of India. District and tehsil polygons: NIC Bharatmaps / Local "
    "Government Directory (MoPR), collated by DataMeet and ramSeraph (CC0). Villages: Census of "
    "India 2011. Places: OpenStreetMap contributors (ODbL), GeoNames (CC BY 4.0), Wikidata (CC0)."
)
MAX_AGE_S = 3600  # the data changes at most weekly
BOUNDARY_SOURCES = ["boundaries", "lgd"]


# --- caching -------------------------------------------------------------------------------------
# Simplifying a whole region costs ~0.1-1 s, and the answer only changes when the boundaries job
# reloads. Cache payloads in-process under the table's current version (row count + max id), so a
# reload is picked up on the next request.

_cache: TTLCache[tuple[Any, ...], dict[str, Any]] = TTLCache(maxsize=64, ttl=6 * 3600)
_tiles: LRUCache[tuple[Any, ...], bytes] = LRUCache(maxsize=2048)
_cache_lock = threading.Lock()


def _version(session: Session) -> tuple[int, int]:
    query = text("SELECT count(*), COALESCE(max(id), 0) FROM boundary")
    count, max_id = session.execute(query).one()
    return int(count), int(max_id)


def _cached(key: tuple[Any, ...], build: Any) -> dict[str, Any]:
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    value = build()
    with _cache_lock:
        _cache[key] = value
    return value


# --- helpers -------------------------------------------------------------------------------------


def _refs(
    session: Session, fetch_ids: Iterable[int | None], keys: Sequence[str]
) -> list[SourceRef]:
    """The exact fetches behind the rows, then the latest fetch of each source in `keys` that is
    not already cited."""
    refs: list[SourceRef] = []
    for fetch_id in sorted({f for f in fetch_ids if f is not None}):
        ref = ref_for_fetch(session, fetch_id)
        if ref is not None:
            refs.append(ref)
    cited = {r.key for r in refs}
    refs += [r for r in latest_refs(session, keys) if r.key not in cited]
    return refs


def _round_coords(geom: BaseGeometry, digits: int) -> dict[str, Any]:
    """GeoJSON MultiPolygon with coordinates rounded to `digits` decimals (numpy arrays, which
    respond() serialises directly)."""
    polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    coordinates = [
        [np.round(np.asarray(ring.coords)[:, :2], digits) for ring in (p.exterior, *p.interiors)]
        for p in polygons
        if not p.is_empty
    ]
    return {"type": "MultiPolygon", "coordinates": coordinates}


def _simplify(geoms: list[BaseGeometry], tolerance: float) -> list[BaseGeometry]:
    """Simplify polygons that share borders as one coverage; fall back to one-by-one."""
    if tolerance <= 0:
        return geoms
    try:
        out = list(shapely.coverage_simplify(np.array(geoms, dtype=object), tolerance))
    except Exception:  # not a clean coverage (or an old GEOS): simplify each polygon alone
        array = np.array(geoms, dtype=object)
        out = list(shapely.simplify(array, tolerance, preserve_topology=True))
    return [o if not o.is_empty else g for o, g in zip(out, geoms, strict=True)]


_STATE_FILTER = """
    AND (CAST(:state AS text) IS NULL OR lower(b.state) = lower(:state) OR b.state IN (
        SELECT s.name FROM boundary s WHERE s.level = 'state'
        AND (s.code = :state OR upper(s.props->>'iso3166_2') = upper(:state))))
"""


# --- /boundaries ---------------------------------------------------------------------------------


@router.get("/boundaries")
@cached_route(ttl=600)  # 1.4 MB of GeoJSON: keep the serialised bytes, not just the payload
def boundaries(
    session: SessionDep,
    bbox: BBoxDep,
    level: Level = "district",
    state: Annotated[str | None, Query(description="state name, LGD code or ISO code")] = None,
    simplify: Annotated[float, Query(ge=0, le=0.05, description="tolerance, degrees")] = 0.001,
) -> Response:
    """Polygons of one level that touch `bbox` (default: the area we watch), for map display.

    Polygons are whole (not clipped to the box). `simplify=0` returns full detail. Each feature
    carries its LGD code, area and, for districts, other spellings of its name (`aliases`).
    """
    # Neon egress: the polygons are read whole (2.9 MB of WKB for the 13 state outlines) and
    # simplified here, so the finished answer is stored (api/cache.stored_response) until the
    # boundary table (cache.table_stamp: any insert, update or delete), the sources' latest
    # fetches or the code (and GEOS, which simplifies) change.
    rows = table_stamp(session, "boundary")
    refs = latest_refs(session, BOUNDARY_SOURCES)
    stamp = (f"{rows}|{','.join(str(r.fetch_id) for r in refs)}|"
             f"{code_version(__name__)}|{shapely.__version__}/{shapely.geos_version_string}")

    def build() -> tuple[dict[str, Any], list[SourceRef], int]:
        key = ("boundaries", rows, level, (state or "").lower(), bbox, simplify)
        payload = _cached(key, lambda: _boundaries_payload(session, bbox, level, state, simplify))
        sources = _refs(session, payload["_ids"], BOUNDARY_SOURCES)
        return {k: v for k, v in payload.items() if not k.startswith("_")}, sources, MAX_AGE_S

    stored_key = canonical_key("geo/boundaries", bbox=bbox, level=level, state=state, simplify=simplify)
    # A whole level is megabytes of rows: a never-stored variant (another `simplify`, another box)
    # counts against the novel-query allowance (api/cache.py); one state's outline is small.
    return stored_response(session, stored_key, stamp, build, guard=state is None)


def _boundaries_payload(
    session: Session, bbox: tuple[float, ...], level: str, state: str | None, tolerance: float
) -> dict[str, Any]:
    # Binary format: the WKB arrives as bytes, not hex text (half the database egress).
    state_filter = re.sub(r":(state)\b", r"%(\1)s", _STATE_FILTER)
    rows = fetch_binary(
        session,
        f"""
        SELECT b.id, b.level, b.name, b.state, b.district, b.code, b.source, b.props,
               b.fetch_id, ST_AsBinary(b.geom) AS wkb
        FROM boundary b
        WHERE b.level = %(level)s
          AND b.geom && ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326)
          AND ST_Intersects(b.geom, ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326))
          {state_filter}
        ORDER BY b.state, b.name
        """,
        {"level": level, "state": state, "x0": bbox[0], "y0": bbox[1], "x1": bbox[2],
         "y1": bbox[3]},
    )
    geoms = _simplify([shapely.from_wkb(r["wkb"]) for r in rows], tolerance)
    digits = 5 if tolerance < 0.0005 else 4  # ~1 m or ~11 m: below the simplification error
    features = [
        {
            "type": "Feature",
            "id": r["id"],
            "geometry": _round_coords(g, digits),
            "properties": _area_properties(r),
        }
        for r, g in zip(rows, geoms, strict=True)
    ]
    return {
        "level": level,
        "state": state,
        "bbox": list(bbox),
        "simplify_deg": tolerance,
        "count": len(features),
        "notes": _notes(session, level, rows),
        "attribution": ATTRIBUTION,
        "features": {"type": "FeatureCollection", "features": features},
        "_ids": [r["fetch_id"] for r in rows],
    }


def _area_properties(row: Any) -> dict[str, Any]:
    props = row["props"] or {}
    out = {
        "id": row["id"],
        "level": row["level"],
        "name": row["name"],
        "state": row["state"],
        "code": row["code"],
        "area_km2": props.get("area_km2"),
    }
    if row["level"] == "subdistrict":
        out["district"] = row["district"]
    if row["level"] == "district":
        out["aliases"] = props.get("aliases", [])
    if row["level"] == "state" and props.get("iso3166_2"):
        out["iso3166_2"] = props["iso3166_2"]
    if props.get("in_lgd_today") is False:
        out["lgd_status"] = "not in today's LGD list"
    if props.get("derived_from_subdistricts"):
        out["derived"] = {
            "from_subdistricts": props["derived_from_subdistricts"],
            "notification": props.get("notification"),
            "note": props.get("note"),
        }
    out["source"] = row["source"]
    return out


def _notes(session: Session, level: str, rows: Sequence[Any]) -> list[str]:
    """Plain-language caveats about the polygons returned (vintage, rebuilt districts, and
    districts that LGD lists but no open polygon exists for), only for states in the answer."""
    notes = []
    if level in ("district", "subdistrict"):
        notes.append(
            "District and tehsil polygons are the LGD/Bharatmaps set of Dec 2023, renamed to "
            "today's LGD names."
        )
    if not rows:
        notes.append(
            "No polygons match: either nothing of this level touches the box or state, or the "
            "boundaries job has not loaded them yet."
        )
    for r in rows:
        p = r["props"] or {}
        if p.get("derived_from_subdistricts"):
            notes.append(
                f"{r['name']} (LGD {r['code']}) has no published polygon yet; it is rebuilt from "
                f"tehsils {p['derived_from_subdistricts']} ({p.get('notification')}): "
                f"{p.get('note')}."
            )
    if level == "district":
        missing = session.execute(
            text(
                "SELECT name, props->'lgd_without_polygon' FROM boundary WHERE level = 'state' "
                "AND name = ANY(:states) "
                "AND jsonb_array_length(COALESCE(props->'lgd_without_polygon', '[]'::jsonb)) > 0"
            ),
            {"states": sorted({r["state"] for r in rows if r["state"]})},
        ).all()
        for state_name, absent in missing:
            names = ", ".join(f"{a['name']} ({a['code']})" for a in absent)
            notes.append(
                f"{state_name}: today's LGD list has districts with no published polygon yet "
                f"({names}); the map shows the older district polygons there."
            )
    return notes


# --- /districts ----------------------------------------------------------------------------------


@router.get("/districts")
def districts(
    session: SessionDep,
    state: Annotated[str | None, Query(description="state name, LGD code or ISO code")] = None,
) -> Response:
    """Every district we have a polygon for (no geometry): name, state, LGD code, other
    spellings, area. Defaults to Punjab, Haryana, Delhi and Chandigarh; `state=all` for all."""
    if state is None:
        where, params = "AND (b.props->>'state_lgd') IN ('3','6','7','4')", {}
    elif state.lower() == "all":
        where, params = "", {}
    else:
        where, params = _STATE_FILTER, {"state": state}
    rows = session.execute(
        text(
            f"""
            SELECT b.id, b.name, b.state, b.code, b.props, b.fetch_id
            FROM boundary b WHERE b.level = 'district' {where}
            ORDER BY b.state, b.name
            """
        ),
        params,
    ).mappings().all()
    items = [
        {
            "id": r["id"],
            "name": r["name"],
            "state": r["state"],
            "code": r["code"],
            "area_km2": (r["props"] or {}).get("area_km2"),
            "aliases": (r["props"] or {}).get("aliases", []),
            "in_lgd_today": (r["props"] or {}).get("in_lgd_today"),
            "derived": bool((r["props"] or {}).get("derived_from_subdistricts")),
        }
        for r in rows
    ]
    return respond(
        {"count": len(items), "districts": items},
        _refs(session, [r["fetch_id"] for r in rows], BOUNDARY_SOURCES),
        max_age=MAX_AGE_S,
    )


# --- /cities -------------------------------------------------------------------------------------


@router.get("/cities")
def cities(session: SessionDep) -> Response:
    """Receptor cities: the most populous city of each district (each union territory) in the
    area we watch with 250,000+ people, plus million-plus cities inside the wind-field box."""
    rows = session.execute(
        text(
            """
            SELECT id, name, state, district, lat, lon, population, props, fetch_id
            FROM place WHERE kind = 'city' ORDER BY population DESC NULLS LAST, name
            """
        )
    ).mappings().all()
    items = []
    fetch_ids: list[int | None] = []
    for r in rows:
        p = r["props"] or {}
        items.append(
            {
                "id": r["id"],
                "name": r["name"],
                "state": r["state"],
                "district": r["district"],
                "lat": r["lat"],
                "lon": r["lon"],
                "population": r["population"],
                "population_source": p.get("population_source"),
                "geonames_id": p.get("geonames_id"),
                "wikidata": p.get("wikidata"),
                "wikidata_population": p.get("wikidata_population"),
                "wikidata_population_date": p.get("wikidata_population_date"),
                "tier": p.get("tier"),
            }
        )
        fetch_ids += [r["fetch_id"], p.get("wikidata_fetch_id")]
    rule = ((rows[0]["props"] or {}).get("rule") if rows else None)
    reason = None if rows else "no receptor cities loaded yet: the boundaries job has not run"
    return respond(
        {"count": len(items), "rule": rule, "cities": items, "reason": reason},
        _refs(session, fetch_ids, ["geonames", "wikidata"]),
        max_age=MAX_AGE_S,
    )


# --- /lookup -------------------------------------------------------------------------------------


@router.get("/lookup")
def lookup(
    session: SessionDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
    max_km: Annotated[float, Query(gt=0, le=200)] = 25,
) -> Response:
    """Where a point is: state, district, tehsil, the nearest village or town (within `max_km`)
    and the nearest receptor city. Unknown parts are null, never guessed."""
    area = district_for_point(session, lon, lat)
    settlement = nearest_place(session, lon, lat, kinds=("village", "town"), max_km=max_km)
    city = nearest_place(session, lon, lat, kinds=("city",))
    found = [p for p in (settlement, city) if p is not None]
    fetch_ids = []
    if found:
        fetch_ids = list(
            session.execute(
                text("SELECT fetch_id FROM place WHERE id = ANY(:ids)"),
                {"ids": [p.id for p in found]},
            ).scalars()
        )
    keys = ["boundaries", "lgd", "census2011", "osm", "geonames"]
    return respond(
        {
            "point": {"lat": lat, "lon": lon},
            "state": area.state if area else None,
            "district": area.district if area else None,
            "subdistrict": area.subdistrict if area else None,
            "nearest_place": _place(settlement),
            "nearest_city": _place(city),
        },
        _refs(session, fetch_ids, keys),
        max_age=MAX_AGE_S,
    )


def _place(p: Any) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "id": p.id, "kind": p.kind, "name": p.name, "district": p.district, "state": p.state,
        "lat": p.lat, "lon": p.lon, "population": p.population,
        "distance_km": round(p.distance_km, 2),
    }


# --- /places -------------------------------------------------------------------------------------


@router.get("/places")
def places(
    session: SessionDep,
    q: Annotated[str, Query(min_length=2, max_length=80)],
    kind: Literal["village", "town", "city"] | None = None,
    state: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Response:
    """Places whose name starts with `q` (exact matches first, then by population)."""
    rows = session.execute(
        text(
            """
            SELECT id, kind, name, subdistrict, district, state, lat, lon, population, source,
                   fetch_id
            FROM place
            WHERE name ILIKE :prefix
              AND (CAST(:kind AS text) IS NULL OR kind = :kind)
              AND (CAST(:state AS text) IS NULL OR lower(state) = lower(:state))
            ORDER BY lower(name) = lower(:q) DESC, population DESC NULLS LAST, name
            LIMIT :limit
            """
        ),
        {"prefix": _escape_like(q.strip()) + "%", "q": q.strip(), "kind": kind, "state": state,
         "limit": limit},
    ).mappings().all()
    items = [{k: v for k, v in r.items() if k != "fetch_id"} for r in rows]
    keys = ["census2011", "osm", "geonames"]
    return respond(
        {"q": q, "count": len(items), "places": items},
        _refs(session, [r["fetch_id"] for r in rows], keys),
        max_age=MAX_AGE_S,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- /tiles --------------------------------------------------------------------------------------

# Which levels a tile carries at a zoom: states always (India's outline), districts from z5,
# tehsils from z8. A deeper level at low zoom would be clutter and slow.
_TILE_LAYERS: tuple[tuple[str, int], ...] = (("state", 0), ("district", 5), ("subdistrict", 8))

_TILE_SQL = text(
    """
    WITH bounds AS (
        SELECT ST_TileEnvelope(:z, :x, :y) AS env,
               ST_Transform(ST_TileEnvelope(:z, :x, :y, margin => 0.015625), 4326) AS env4326
    ), features AS (
        SELECT b.id, b.name, b.state, b.district, b.code,
               ST_AsMVTGeom(
                   ST_Transform(ST_Simplify(ST_ClipByBox2D(b.geom, bounds.env4326), :tol), 3857),
                   bounds.env, 4096, 64, true) AS geom
        FROM boundary b, bounds
        WHERE b.level = :level AND b.geom && bounds.env4326
    )
    SELECT ST_AsMVT(features, :layer, 4096, 'geom', 'id') FROM features WHERE geom IS NOT NULL
    """
)


@router.get("/tiles/{z}/{x}/{y}.pbf")
def tiles(
    session: SessionDep,
    z: Annotated[int, Path(ge=0, le=14)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
) -> Response:
    """Mapbox Vector Tile with layers `states` (Survey of India: draw India's outline and state
    lines from this, never from the basemap), `districts` (z5+) and `subdistricts` (z8+).
    Feature id = boundary id; properties: name, state, district, code. 204 when empty."""
    if x >= 2**z or y >= 2**z:
        raise HTTPException(404, "tile outside the zoom level's grid")
    key = (_version(session), z, x, y)
    with _cache_lock:
        tile = _tiles.get(key)
    if tile is None:
        # Simplify to about half a tile pixel before clipping: invisible, and much less to clip.
        tolerance = 360.0 / (2**z) / 4096 / 2
        parts = []
        for level, min_zoom in _TILE_LAYERS:
            if z < min_zoom:
                continue
            layer = {"state": "states", "district": "districts", "subdistrict": "subdistricts"}
            blob = session.execute(
                _TILE_SQL,
                {"z": z, "x": x, "y": y, "tol": tolerance, "level": level, "layer": layer[level]},
            ).scalar()
            if blob:
                parts.append(bytes(blob))
        tile = b"".join(parts)  # MVT layers concatenate into one valid tile
        with _cache_lock:
            _tiles[key] = tile
    headers = {"Cache-Control": f"public, max-age={MAX_AGE_S}"}
    if not tile:
        return Response(status_code=204, headers=headers)
    return Response(tile, media_type="application/vnd.mapbox-vector-tile", headers=headers)
