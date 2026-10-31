"""Job `boundaries` (weekly): which state, district and tehsil a point is in, and what is near it.

Steps, each one standing alone (an upstream outage fails that step only; its rows from the last
good run stay in place, and the job fails only when every step fails):

1. areas      Survey of India states (the whole country: the map draws India's outline from them),
              LGD/Bharatmaps districts touching the wind-field box (so smoke that leaves Punjab and
              Haryana is still named) and tehsils of Punjab, Haryana, Delhi and Chandigarh, all
              named the way the Local Government Directory names them today. Hansi (Haryana, Dec
              2025) has no open polygon yet, so it is rebuilt from the tehsils it was formed from.
              Polygons are stored simplified to ~20 m, India's outline exact (sources/boundaries.py).
2. villages   Census 2011 village points with population (place, kind 'village').
3. osm        OpenStreetMap towns, plus OSM villages the census list does not have.
4. cities     Receptor cities: the most populous GeoNames city of each district (place, 'city').
5. labels     State / district / tehsil on every place, district aliases, then geo.fill_admin()
              on other modules' tables (fire_detection, burn_cluster, ...).

Writes replace this job's own rows in one transaction, so a rerun is idempotent and readers
never see a half-loaded table. Areas are rewritten only when an input file or LGD list changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import shapely
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.config import BBox, get_settings
from blindspot.db import session_scope
from blindspot.geo import fill_admin
from blindspot.http import FetchError, set_fetch_records
from blindspot.jobs import job
from blindspot.sources import boundaries as src

log = logging.getLogger(__name__)

# Bump when the way areas are built changes, so the next run reloads them even if no input did.
LOADER_VERSION = "3"

# `source` of the rows this job writes. Each run replaces its own rows by these values.
SRC_STATES = "soi_states"
SRC_DISTRICTS = "lgd_districts_2023"
SRC_SUBDISTRICTS = "lgd_subdistricts_2023"
SRC_DERIVED = "lgd_derived"
AREA_SOURCES = [SRC_STATES, SRC_DISTRICTS, SRC_SUBDISTRICTS, SRC_DERIVED]
PLACE_CENSUS = "census2011"
PLACE_OSM = "osm"
PLACE_CITIES = "geonames"
PLACE_SOURCES = [PLACE_CENSUS, PLACE_OSM, PLACE_CITIES]

# Receptor cities. Inside the area we watch (settings.aoi_bbox) a city of at least a quarter of a
# million people; elsewhere inside the wind-field box (settings.met_bbox) a million-plus city.
# Then one city per district (one per union territory: Delhi and Chandigarh are one city each),
# so Delhi's localities (Najafgarh, Narela, ...) do not each become a "city".
MIN_POP_AOI = 250_000
MIN_POP_REGION = 1_000_000
CITY_RULE = (
    f"the most populous GeoNames city of each district (of each union territory) with at least "
    f"{MIN_POP_AOI:,} people inside the area watched, or {MIN_POP_REGION:,} elsewhere in the "
    f"wind-field box"
)

# An OSM village is skipped when the census already has a village this close (same settlement).
OSM_DUPLICATE_M = 1000
OSM_SAME_NAME_DUPLICATE_M = 3000


@dataclass(frozen=True)
class DerivedDistrict:
    code: int  # LGD code of the new district
    subdistricts: tuple[int, ...]  # LGD codes of the 2023 tehsils it was formed from
    notification: str  # the order that created it
    note: str  # what the rebuilt polygon leaves out


# Districts LGD lists today that no open polygon set has yet, rebuilt from the 2023 tehsil polygons
# they were formed from (docs/research/geography-and-basemaps.md, sections 3 and 5). The job checks
# the list against today's LGD data and skips an entry that no longer matches.
DERIVED_DISTRICTS = (
    DerivedDistrict(
        code=792,  # Hansi, Haryana: tehsils Hansi (396) and Narnaund (395), Hisar district in 2023
        subdistricts=(395, 396),
        notification="Haryana Government gazette 682-ARIC-03-2025/7392 of 19 Dec 2025",
        note=(
            "union of its two founding tehsils; a few villages that moved in from Hisar and "
            "Bawani Khera tehsils are not included"
        ),
    ),
)


@job("boundaries", every_minutes=7 * 24 * 60)
def boundaries() -> dict[str, Any]:
    """Weekly: SOI/LGD areas, census villages, OSM towns, receptor cities -> boundary, place."""
    summary: dict[str, Any] = {}
    failed: dict[str, str] = {}
    areas = _step("areas", load_areas, summary, failed)
    _step("villages", load_villages, summary, failed)
    _step("osm", load_osm_places, summary, failed)
    _step("cities", load_cities, summary, failed)
    relabel_all = bool(areas and areas.get("reloaded"))
    _step("labels", lambda: label_everything(relabel_all), summary, failed)
    if failed:
        summary["failed"] = failed
    if len(failed) == 5:
        raise RuntimeError(f"every step failed: {failed}")
    return summary


def _step(
    name: str, run: Callable[[], dict[str, Any]], summary: dict[str, Any], failed: dict[str, str]
) -> dict[str, Any] | None:
    """Run one step, timing it; an exception fails this step only (logged with its traceback)."""
    t0 = time.perf_counter()
    try:
        result = run()
    except Exception as exc:
        log.exception("boundaries: step %s failed", name)
        failed[name] = f"{type(exc).__name__}: {exc}"[:500]
        return None
    result["seconds"] = round(time.perf_counter() - t0, 1)
    summary[name] = result
    log.info("boundaries: %s %s", name, result)
    return result


# --- 1. areas ------------------------------------------------------------------------------------


@dataclass
class Area:
    level: str
    code: str | None
    name: str
    state: str | None
    district: str | None
    geom: BaseGeometry
    source: str
    props: dict[str, Any]
    fetch_id: int | None


def load_areas() -> dict[str, Any]:
    """States, districts and tehsils -> boundary (all inputs must load, or nothing changes)."""
    settings = get_settings()
    lgd = src.fetch_lgd_today()
    files = {
        "states": src.download("boundaries", src.SOI_STATES_URL),
        "districts": src.download("boundaries", src.LGD_DISTRICTS_URL),
        "subdistricts": src.download("boundaries", src.LGD_SUBDISTRICTS_URL),
    }
    inputs = hashlib.sha256(
        "|".join([LOADER_VERSION, lgd.sha256, *(f.sha256 for f in files.values())]).encode()
    ).hexdigest()
    report: dict[str, Any] = {
        "lgd_observed_on": lgd.observed_on,
        "files": {
            k: {"sha256": f.sha256[:12], "not_modified": f.not_modified}
            for k, f in files.items()
        },
    }
    with session_scope() as session:
        current = session.execute(
            text("SELECT props->>'inputs' FROM boundary WHERE source = :s LIMIT 1"),
            {"s": SRC_STATES},
        ).scalar()
    if current == inputs:
        counts = _area_counts()
        # The files answered 304: record how many current rows each one still backs, so the
        # sources panel shows the data as present rather than "0 records".
        for kind, level in (("states", "state"), ("districts", "district"),
                            ("subdistricts", "subdistrict")):
            set_fetch_records(files[kind].fetch_id, counts["total"].get(level, 0))
        report.update(reloaded=False, counts=counts)
        return report

    states = _state_areas(src.read_soi_states(files["states"].path), lgd, files["states"].fetch_id)
    state_names = {a.props["lgd"]: a.name for a in states if a.props.get("lgd")}
    districts = _district_areas(
        src.read_lgd_districts(files["districts"].path, settings.met_bbox),
        lgd, state_names, files["districts"].fetch_id,
    )
    subdistricts = _subdistrict_areas(
        src.read_lgd_subdistricts(files["subdistricts"].path, settings.met_bbox, src.AOI_STATES),
        lgd, state_names, files["subdistricts"].fetch_id,
    )
    # Stored simplified, India's outline exact (sources/boundaries.py explains both limits).
    band = src.outline_band([a.geom for a in states], settings.met_bbox)
    for area in (*states, *districts, *subdistricts):
        area.props["inputs"] = inputs
        area.geom = src.simplify_area(area.geom, band)
    with session_scope() as session:
        session.execute(text("DELETE FROM boundary WHERE source = ANY(:s)"), {"s": AREA_SOURCES})
        _insert_areas(session, [*states, *districts, *subdistricts])
        derived = [_derive_district(session, d, lgd, inputs) for d in DERIVED_DISTRICTS]
        session.execute(_SET_AREA_KM2, {"s": AREA_SOURCES})
    mismatches = _lgd_mismatches(lgd)
    _note_missing_on_states(mismatches["lgd_districts_without_polygon"])
    report.update(reloaded=True, derived=derived, counts=_area_counts(), **mismatches)
    loaded = {"states": len(states), "districts": len(districts), "subdistricts": len(subdistricts)}
    for kind, f in files.items():
        set_fetch_records(f.fetch_id, loaded[kind])
    return report


def _state_areas(frame: gpd.GeoDataFrame, lgd: src.LgdToday, fetch_id: int | None) -> list[Area]:
    areas = []
    for row in frame.itertuples(index=False):
        code = int(row.State_LGD or 0)
        unit = lgd.states.get(code) if code else None
        # 4 polygons are inter-state disputed areas with no LGD code; keep SOI's own label.
        name = unit.name if unit else row.STATE_C.strip().title()
        props = {
            "lgd": code or None,
            "name_in_dataset": row.STATE_C,
            "ut": unit.is_ut if unit else None,
        }
        if code in src.AOI_STATES:
            props["iso3166_2"] = src.AOI_STATES[code]
        areas.append(Area("state", str(code) if code else None, name, name, None, row.geometry,
                          SRC_STATES, props, fetch_id))
    return _dissolve(areas)


def _district_areas(
    frame: gpd.GeoDataFrame, lgd: src.LgdToday, state_names: dict[int, str], fetch_id: int | None
) -> list[Area]:
    areas = []
    for row in frame.itertuples(index=False):
        code, state_code = int(row.dist_lgd), int(row.state_lgd)
        today = lgd.district_name(code)
        dataset_name = row.dtname.strip()
        props: dict[str, Any] = {
            "lgd": code,
            "state_lgd": state_code,
            "census2011": (row.dtcode11 or "").strip() or None,
            "dataset_vintage": row.year_stat,
            "in_lgd_today": today is not None,
        }
        if today and today != dataset_name:
            props["name_in_dataset"] = dataset_name
        areas.append(Area("district", str(code), today or dataset_name,
                          state_names.get(state_code), None, row.geometry, SRC_DISTRICTS, props,
                          fetch_id))
    return _dissolve(areas)


def _subdistrict_areas(
    frame: gpd.GeoDataFrame, lgd: src.LgdToday, state_names: dict[int, str], fetch_id: int | None
) -> list[Area]:
    areas = []
    for row in frame.itertuples(index=False):
        code = int(row.subdt_lgd)
        unit = lgd.subdistricts.get(code)
        dataset_name = row.sdtname.strip()
        # Today's parent district: Hansi and Narnaund tehsils now belong to Hansi, not Hisar.
        parent = unit.parent if unit else int(row.dist_lgd)
        props: dict[str, Any] = {
            "lgd": code,
            "district_lgd": parent,
            "census2011": (row.sdtcode11 or "").strip() or None,
            "in_lgd_today": unit is not None,
        }
        if unit and unit.name != dataset_name:
            props["name_in_dataset"] = dataset_name
        district = lgd.district_name(parent) or row.dtname.strip()
        areas.append(Area("subdistrict", str(code), unit.name if unit else dataset_name,
                          state_names.get(int(row.state_lgd)), district, row.geometry,
                          SRC_SUBDISTRICTS, props, fetch_id))
    return _dissolve(areas)


def _dissolve(areas: list[Area]) -> list[Area]:
    """Merge rows that repeat a code into one (LGD has Loharu tehsil 401 in two pieces)."""
    merged: dict[tuple[str, str | None], Area] = {}
    out: list[Area] = []
    for area in areas:
        key = (area.level, area.code)
        if area.code is None or key not in merged:
            merged[key] = area
            out.append(area)
            continue
        first = merged[key]
        first.geom = shapely.union_all(shapely.make_valid([first.geom, area.geom]))
        first.props["dissolved_rows"] = first.props.get("dissolved_rows", 1) + 1
    return out


# Every polygon is made valid in PostGIS and stored as a MultiPolygon (the column type).
_INSERT_AREA = text(
    """
    INSERT INTO boundary (level, name, state, district, code, geom, source, props, fetch_id)
    VALUES (:level, :name, :state, :district, :code,
            ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(:wkb, 4326)), 3)),
            :source, CAST(:props AS jsonb), :fetch_id)
    """
)
_SET_AREA_KM2 = text(
    """
    UPDATE boundary
    SET props = props || jsonb_build_object(
        'area_km2', round((ST_Area(geom::geography) / 1e6)::numeric, 1))
    WHERE source = ANY(:s)
    """
)


def _insert_areas(session: Session, areas: Iterable[Area]) -> None:
    rows = [
        {
            "level": a.level, "name": a.name, "state": a.state, "district": a.district,
            "code": a.code, "wkb": a.geom.wkb, "source": a.source,
            "props": json.dumps(a.props), "fetch_id": a.fetch_id,
        }
        for a in areas
    ]
    for start in range(0, len(rows), 50):  # small batches: some state polygons are megabytes
        session.execute(_INSERT_AREA, rows[start : start + 50])


def _derive_district(
    session: Session, spec: DerivedDistrict, lgd: src.LgdToday, inputs: str
) -> dict[str, Any]:
    """Build a new district from its 2023 tehsils and cut it out of the district(s) it left."""
    unit = lgd.districts.get(spec.code)
    if unit is None:
        return {"code": spec.code, "skipped": "not in today's LGD list"}
    have_polygon = {
        int(c) for c in session.execute(
            text("SELECT code FROM boundary WHERE level = 'subdistrict' AND source = :s"),
            {"s": SRC_SUBDISTRICTS},
        ).scalars()
    }
    listed = {u.code for u in lgd.subdistricts_of(spec.code)} & have_polygon
    if listed != set(spec.subdistricts):
        # LGD now puts different old tehsils in this district: the recipe is out of date.
        return {"code": spec.code, "skipped": f"LGD lists tehsils {sorted(listed)} with polygons"}
    state_unit = lgd.states.get(unit.parent or 0)
    state = state_unit.name if state_unit else None
    props = {
        "lgd": spec.code, "state_lgd": unit.parent, "in_lgd_today": True, "inputs": inputs,
        "derived_from_subdistricts": list(spec.subdistricts),
        "notification": spec.notification, "note": spec.note,
    }
    new_id = session.execute(
        text(
            """
            INSERT INTO boundary (level, name, state, district, code, geom, source, props, fetch_id)
            SELECT 'district', :name, :state, NULL, :code,
                   ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(geom)), 3)),
                   :source, CAST(:props AS jsonb), max(fetch_id)
            FROM boundary
            WHERE level = 'subdistrict' AND source = :sub AND code = ANY(:codes)
            RETURNING id
            """
        ),
        {
            "name": unit.name, "state": state, "code": str(spec.code), "source": SRC_DERIVED,
            "props": json.dumps(props), "sub": SRC_SUBDISTRICTS,
            "codes": [str(c) for c in spec.subdistricts],
        },
    ).scalar_one()
    # Cut it out of every district it overlaps by more than 1 km2 (edge noise is left alone).
    carved = session.execute(
        text(
            """
            UPDATE boundary d
            SET geom = ST_Multi(ST_CollectionExtract(
                    ST_MakeValid(ST_Difference(d.geom, n.geom)), 3)),
                props = d.props || jsonb_build_object('carved_out', n.code)
            FROM boundary n
            WHERE n.id = :id AND d.level = 'district' AND d.id <> n.id
              AND ST_Intersects(d.geom, n.geom)
              AND ST_Area(ST_Intersection(d.geom, n.geom)::geography) > 1e6
            RETURNING d.name
            """
        ),
        {"id": new_id},
    ).scalars().all()
    return {"code": spec.code, "name": unit.name, "carved_from": carved}


def _area_counts() -> dict[str, dict[str, int]]:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT level, COALESCE(state, '?') AS state, count(*) FROM boundary
                WHERE source = ANY(:s)
                  AND (level <> 'district' OR (props->>'state_lgd')::int = ANY(:aoi))
                GROUP BY level, state ORDER BY level, state
                """
            ),
            {"s": AREA_SOURCES, "aoi": list(src.AOI_STATES)},
        ).all()
        totals = session.execute(
            text("SELECT level, count(*) FROM boundary WHERE source = ANY(:s) GROUP BY level"),
            {"s": AREA_SOURCES},
        ).all()
    counts: dict[str, dict[str, int]] = {"total": {lvl: n for lvl, n in totals}}
    for level, state, n in rows:
        if level != "state":
            counts.setdefault(level, {})[state] = n
    return counts


def _lgd_mismatches(lgd: src.LgdToday) -> dict[str, list[dict[str, Any]]]:
    """In our states: districts LGD lists today with no polygon (Delhi's three Dec-2025
    districts), and polygons whose code LGD no longer lists (Shahdara). Reported, never hidden."""
    with session_scope() as session:
        have = dict(
            session.execute(
                text("SELECT code::int, name FROM boundary WHERE level = 'district' "
                     "AND source = ANY(:s) AND (props->>'state_lgd')::int = ANY(:aoi)"),
                {"s": AREA_SOURCES, "aoi": list(src.AOI_STATES)},
            ).all()
        )
    unit_state = {code: lgd.states[code].name for code in src.AOI_STATES if code in lgd.states}
    return {
        "lgd_districts_without_polygon": [
            {"code": u.code, "name": u.name, "state": unit_state.get(u.parent),
             "state_lgd": u.parent}
            for u in lgd.districts.values()
            if u.parent in src.AOI_STATES and u.code not in have
        ],
        "polygons_not_in_lgd_today": [
            {"code": code, "name": name} for code, name in have.items() if code not in lgd.districts
        ],
    }


def _note_missing_on_states(absent: list[dict[str, Any]]) -> None:
    """Keep the "LGD lists it, no polygon exists" districts on their state's row, so the API can
    say so next to the map (Delhi has 13 districts since Dec 2025; open polygons exist for 11)."""
    with session_scope() as session:
        for code in src.AOI_STATES:
            mine = [
                {"code": a["code"], "name": a["name"]} for a in absent if a["state_lgd"] == code
            ]
            session.execute(
                text(
                    "UPDATE boundary SET props = props || jsonb_build_object("
                    "'lgd_without_polygon', CAST(:a AS jsonb)) "
                    "WHERE level = 'state' AND code = :c AND source = :s"
                ),
                {"a": json.dumps(mine), "c": str(code), "s": SRC_STATES},
            )


# --- 2. census villages --------------------------------------------------------------------------

_INSERT_PLACE = text(
    """
    INSERT INTO place (kind, name, lat, lon, geom, population, source, props, fetch_id)
    VALUES (:kind, :name, :lat, :lon, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :population,
            :source, CAST(:props AS jsonb), :fetch_id)
    """
)


def load_villages() -> dict[str, Any]:
    """Census 2011 village points of Punjab, Haryana, Delhi and Chandigarh -> place."""
    settings = get_settings()
    file = src.download("census2011", src.CENSUS_VILLAGES_URL)
    frame = src.read_census_villages(file.path, settings.met_bbox, src.AOI_STATES)
    rows = []
    for row in frame.itertuples(index=False):
        point = row.geometry
        if point is None or point.is_empty:
            continue
        census_name = _text(row.vilname)
        if not census_name:
            continue
        population = _int(row.t_pop2011)
        population = population if population and population > 0 else None  # 0 = not reported
        props = {
            "census2011_code": row.vilcode11,
            "census_name": census_name,
            "census_district": _text(row.dtname),
            "lgd_district": _int(row.DT_LGD),
            "lgd_subdistrict": _int(row.SDT_LGD),
            "gram_panchayat": _text(row.GP_NAME),
            "population_year": 2011 if population else None,
        }
        remark = _text(row.Remarks)
        if remark:
            props["remark"] = remark  # e.g. "Positional Accuracy needs further verification"
        rows.append(
            {
                "kind": "village", "name": src.village_display_name(census_name),
                "lat": float(point.y), "lon": float(point.x), "population": population,
                "source": PLACE_CENSUS, "props": json.dumps(props), "fetch_id": file.fetch_id,
            }
        )
    with session_scope() as session:
        session.execute(text("DELETE FROM place WHERE source = :s"), {"s": PLACE_CENSUS})
        session.execute(_INSERT_PLACE, rows)
    src.set_fetch_records(file.fetch_id, len(rows))
    by_state = frame.groupby("ST_LGD").size().to_dict()
    return {
        "villages": len(rows),
        "by_state_lgd": {int(k): int(v) for k, v in by_state.items()},
        "with_population": sum(1 for r in rows if r["population"]),
        "not_modified": file.not_modified,
    }


def _int(value: Any) -> int | None:
    """An int from a parquet cell that may be None or NaN."""
    try:
        return None if value is None or value != value else int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


# --- 3. OpenStreetMap ----------------------------------------------------------------------------


def load_osm_places() -> dict[str, Any]:
    """OSM towns of each state, plus OSM villages the census list has no village for nearby.

    One Overpass query per state, so a failure in one state keeps the others' fresh rows (and
    that state's rows from the last good run).
    """
    report: dict[str, Any] = {}
    errors = {}
    for iso in src.AOI_STATES.values():
        try:
            result = src.fetch_osm_places(iso)
        except FetchError as exc:
            errors[iso] = str(exc)[:300]
            continue
        report[iso] = _store_osm(iso, result)
    if errors:
        report["errors"] = errors
    if not any(k.startswith("IN-") for k in report):
        raise FetchError("osm", f"no state could be fetched: {errors}")
    return report


def _store_osm(iso: str, result: src.OsmResult) -> dict[str, Any]:
    rows = []
    for p in result.places:
        kind = "town" if p.place in ("city", "town") else "village"
        tags = {k: v for k, v in p.tags.items() if k not in ("place", "population")}
        props = {
            "osm": p.osm_id, "osm_place": p.place, "osm_area": iso, "osm_base": result.osm_base,
            **{f"tag_{k.replace(':', '_')}": v for k, v in tags.items()},
        }
        rows.append({"kind": kind, "name": p.name, "lat": p.lat, "lon": p.lon,
                     "population": p.population, "props": json.dumps(props),
                     "fetch_id": result.fetch_id})
    with session_scope() as session:
        session.execute(
            text("CREATE TEMP TABLE osm_stage (kind text, name text, lat float8, lon float8, "
                 "population int, props jsonb, fetch_id bigint) ON COMMIT DROP")
        )
        session.execute(
            text("INSERT INTO osm_stage VALUES (:kind, :name, :lat, :lon, :population, "
                 "CAST(:props AS jsonb), :fetch_id)"),
            rows,
        )
        session.execute(
            text("DELETE FROM place WHERE source = :s AND props->>'osm_area' = :iso"),
            {"s": PLACE_OSM, "iso": iso},
        )
        inserted = session.execute(
            text(
                """
                INSERT INTO place (kind, name, lat, lon, geom, population, source, props, fetch_id)
                SELECT s.kind, s.name, s.lat, s.lon, g.geom, s.population, :src, s.props, s.fetch_id
                FROM osm_stage s
                CROSS JOIN LATERAL (SELECT ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326) AS geom) g
                WHERE s.kind = 'town' OR NOT EXISTS (
                    SELECT 1 FROM place c
                    WHERE c.source = :census AND ST_DWithin(c.geom, g.geom, 0.04)
                      AND (ST_DWithin(c.geom::geography, g.geom::geography, :near)
                           OR (lower(c.name) = lower(s.name)
                               AND ST_DWithin(c.geom::geography, g.geom::geography, :same)))
                )
                RETURNING kind
                """
            ),
            {"src": PLACE_OSM, "census": PLACE_CENSUS, "near": OSM_DUPLICATE_M,
             "same": OSM_SAME_NAME_DUPLICATE_M},
        ).scalars().all()
    towns = sum(1 for k in inserted if k == "town")
    return {
        "fetched": len(rows), "towns": towns, "villages_added": len(inserted) - towns,
        "osm_base": result.osm_base, "endpoint": result.endpoint,
    }


# --- 4. receptor cities --------------------------------------------------------------------------


def load_cities() -> dict[str, Any]:
    """Receptor cities: GeoNames candidates, Wikidata cross-reference, one per district."""
    settings = get_settings()
    geonames = src.fetch_geonames_cities(settings.met_bbox, MIN_POP_AOI)
    aoi = settings.aoi_bbox
    candidates = [
        c for c in geonames.cities
        if _inside(c.lon, c.lat, aoi) or c.population >= MIN_POP_REGION
    ]
    wikidata: dict[int, src.WikidataCity] = {}
    wikidata_fetch: int | None = None
    wikidata_error = None
    try:
        wd = src.fetch_wikidata_cities([c.geonames_id for c in candidates])
        wikidata, wikidata_fetch = wd.by_geonames_id, wd.fetch_id
        if wd.cached_error:
            wikidata_error = f"used the answer of fetch {wd.fetch_id}: {wd.cached_error}"
    except FetchError as exc:  # Wikidata only adds a cross-reference; carry on without it
        wikidata_error = str(exc)[:300]
    stage = []
    for c in candidates:
        wd_city = wikidata.get(c.geonames_id)
        props = {
            "geonames_id": c.geonames_id, "feature_code": c.feature_code,
            "geonames_modified": c.modified, "population_source": "geonames",
            "tier": "aoi" if _inside(c.lon, c.lat, aoi) else "region",
            "rule": CITY_RULE,
        }
        if wd_city:
            props.update(wikidata=wd_city.qid, wikidata_label=wd_city.label,
                         wikidata_population=wd_city.population,
                         wikidata_population_date=wd_city.population_date,
                         wikidata_fetch_id=wikidata_fetch)
        stage.append({
            "gid": c.geonames_id, "name": c.name, "lat": c.lat, "lon": c.lon,
            "population": c.population, "wd_pop": wd_city.population if wd_city else None,
            "n_alt": len(c.alternate_names), "props": json.dumps(props),
            "fetch_id": geonames.fetch_id,
        })
    with session_scope() as session:
        has_districts = session.execute(
            text("SELECT EXISTS (SELECT 1 FROM boundary WHERE level = 'district')")
        ).scalar()
        if not has_districts:
            raise RuntimeError("receptor cities are chosen per district: load areas first")
        session.execute(
            text("CREATE TEMP TABLE city_stage (gid bigint, name text, lat float8, lon float8, "
                 "population bigint, wd_pop bigint, n_alt int, props jsonb, fetch_id bigint) "
                 "ON COMMIT DROP")
        )
        session.execute(
            text("INSERT INTO city_stage VALUES (:gid, :name, :lat, :lon, :population, :wd_pop, "
                 ":n_alt, CAST(:props AS jsonb), :fetch_id)"),
            stage,
        )
        session.execute(text("DELETE FROM place WHERE source = :s"), {"s": PLACE_CITIES})
        chosen = session.execute(_CHOOSE_CITIES, {"src": PLACE_CITIES}).all()
    return {
        "candidates": len(candidates),
        "cities": [name for name, _ in sorted(chosen, key=lambda r: -r[1])],
        "wikidata_matched": len(wikidata),
        **({"wikidata_error": wikidata_error} if wikidata_error else {}),
    }


# One city per group: a district, or a whole union territory (Delhi NCT, Chandigarh). Ties go to
# the larger Wikidata population, then to the better-known name (more alternate names).
_CHOOSE_CITIES = text(
    """
    WITH located AS (
        SELECT s.*, g.geom, d.code AS district_code, d.state,
               COALESCE((st.props->>'ut')::boolean, false) AS in_ut
        FROM city_stage s
        CROSS JOIN LATERAL (SELECT ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326) AS geom) g
        JOIN LATERAL (
            SELECT b.code, b.state FROM boundary b
            WHERE b.level = 'district' AND ST_Intersects(b.geom, g.geom) ORDER BY b.id LIMIT 1
        ) d ON true
        LEFT JOIN LATERAL (
            SELECT b.props FROM boundary b
            WHERE b.level = 'state' AND b.name = d.state ORDER BY b.id LIMIT 1
        ) st ON true
    ), chosen AS (
        SELECT DISTINCT ON (grp) * FROM (
            SELECT *,
                   CASE WHEN in_ut THEN 'ut:' || state ELSE 'district:' || district_code END AS grp
            FROM located
        ) x
        ORDER BY grp, population DESC, wd_pop DESC NULLS LAST, n_alt DESC, gid
    )
    INSERT INTO place (kind, name, lat, lon, geom, population, source, props, fetch_id)
    SELECT 'city', name, lat, lon, geom, population, :src, props, fetch_id FROM chosen
    RETURNING name, population
    """
)


def _inside(lon: float, lat: float, bbox: BBox) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


# --- 5. labels -----------------------------------------------------------------------------------

_LABEL_PLACES = (
    text("UPDATE place SET state = NULL, district = NULL, subdistrict = NULL "
         "WHERE source = ANY(:s)"),
    text(
        """
        UPDATE place p SET state = b.state, district = b.name
        FROM boundary b
        WHERE p.source = ANY(:s) AND b.level = 'district' AND ST_Intersects(b.geom, p.geom)
        """
    ),
    text(
        """
        UPDATE place p SET subdistrict = b.name
        FROM boundary b
        WHERE p.source = ANY(:s) AND b.level = 'subdistrict' AND ST_Intersects(b.geom, p.geom)
        """
    ),
    text(
        """
        UPDATE place p SET state = b.name
        FROM boundary b
        WHERE p.source = ANY(:s) AND p.state IS NULL AND b.level = 'state'
          AND ST_Intersects(b.geom, p.geom)
        """
    ),
)

# Other spellings of each district, so names from other sources (CREAMS, CPCB, news, a user's
# question) can be matched: the 2023 dataset's spelling, and the district name the census-village
# file gives the villages it holds (Ferozepur <- Firozpur, Charkhi Dadri <- Charki Dadri, as of the
# 2026-10-30 load; that file already uses today's names such as Gurugram and Nuh, so the pre-2016
# names Gurgaon and Mewat are not among the aliases). A census name goes
# only to the district holding most of its villages: after a split (Bhiwani -> Charkhi Dadri,
# Sangrur -> Malerkotla) the new district must not inherit its parent's name.
_DISTRICT_ALIASES = text(
    """
    WITH census AS (
        SELECT p.props->>'census_district' AS alias, d.id, count(*) AS villages
        FROM place p
        JOIN boundary d ON d.level = 'district' AND d.source = ANY(:s)
                       AND d.name = p.district AND d.state = p.state
        WHERE p.source = :census AND p.props ? 'census_district'
          AND strpos(p.props->>'census_district', '*') = 0  -- '*' marks a name cut short
        GROUP BY 1, 2
        HAVING count(*) >= 10  -- a handful of mis-coded points is not a name
    ), majority AS (
        SELECT DISTINCT ON (alias) alias, id FROM census ORDER BY alias, villages DESC
    ), names AS (
        SELECT id, alias FROM majority
        UNION
        SELECT id, props->>'name_in_dataset' FROM boundary
        WHERE level = 'district' AND source = ANY(:s) AND props ? 'name_in_dataset'
    ), grouped AS (
        SELECT n.id, jsonb_agg(DISTINCT n.alias ORDER BY n.alias) AS aliases
        FROM names n JOIN boundary b ON b.id = n.id
        WHERE n.alias IS NOT NULL AND lower(n.alias) <> lower(b.name)
        GROUP BY n.id
    )
    UPDATE boundary d SET props = d.props || jsonb_build_object('aliases', g.aliases)
    FROM grouped g WHERE d.id = g.id
    """
)

# Tables of other modules that fill_admin() can label: a geometry `geom` plus state and district.
_LABELLABLE = text(
    """
    SELECT table_name FROM information_schema.columns
    WHERE table_schema = 'public'
      AND ((column_name = 'geom' AND udt_name = 'geometry') OR column_name IN ('state', 'district'))
      AND table_name NOT IN ('boundary', 'place')
    GROUP BY table_name HAVING count(*) = 3
    ORDER BY table_name
    """
)


def label_everything(relabel_all: bool) -> dict[str, Any]:
    """Label places, record district aliases, then label other modules' rows.

    After boundaries changed (`relabel_all`) every row of other tables is relabelled; otherwise
    only rows still missing a district are. Each table is its own short transaction with a lock
    timeout, so a busy table is skipped this week rather than blocking another module's job.
    """
    report: dict[str, Any] = {}
    with session_scope() as session:
        for statement in _LABEL_PLACES:
            session.execute(statement, {"s": PLACE_SOURCES})
        session.execute(_DISTRICT_ALIASES, {"census": PLACE_CENSUS, "s": AREA_SOURCES})
        report["places"] = dict(
            session.execute(
                text("SELECT kind, count(*) FROM place WHERE source = ANY(:s) "
                     "AND district IS NOT NULL GROUP BY kind"),
                {"s": PLACE_SOURCES},
            ).all()
        )
        tables = session.execute(_LABELLABLE).scalars().all()
    labelled: dict[str, Any] = {}
    for table in tables:
        try:
            with session_scope() as session:
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                session.execute(text("SET LOCAL statement_timeout = '300s'"))
                labelled[table] = fill_admin(session, table, only_missing=not relabel_all)
                labelled[table] += _fill_subdistrict(session, table, relabel_all)
        except Exception as exc:
            labelled[table] = f"skipped: {type(exc).__name__}: {str(exc)[:200]}"
    report["tables"] = labelled
    report["relabel_all"] = relabel_all
    return report


def _fill_subdistrict(session: Session, table: str, relabel_all: bool) -> int:
    """Like geo.fill_admin, for tables that also have a `subdistrict` column (burn_cluster)."""
    has_column = session.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
             "AND table_name = :t AND column_name = 'subdistrict'"),
        {"t": table},
    ).scalar()
    if not has_column:
        return 0
    quoted = session.get_bind().dialect.identifier_preparer.quote(table)
    # Rows whose name would not change are left alone: an UPDATE writes a new row version, and
    # rewriting all 240k scar and field rows every reload bloats a small hosted database.
    missing = "AND t.subdistrict IS DISTINCT FROM b.name" if relabel_all else "AND t.subdistrict IS NULL"
    return session.execute(
        text(
            f"""
            UPDATE {quoted} AS t SET subdistrict = b.name
            FROM boundary AS b
            WHERE b.level = 'subdistrict'
              AND ST_Intersects(b.geom, ST_PointOnSurface(t.geom)) {missing}
            """
        )
    ).rowcount
