"""Tests for the boundaries module (sources/boundaries.py, jobs/boundaries.py, api/routers/geo.py).

Fixtures in tests/fixtures/boundaries/ are recorded real data (all fetched 2026-10-30):
- overpass_in_ch.json: the Overpass answer to the job's places query for Chandigarh
- geonames_cities15000_excerpt.txt: 12 unmodified rows of GeoNames cities15000.txt
- wikidata_cities.json: a Wikidata Query Service answer for 6 GeoNames ids
- lgd_districts_extract.geojson, lgd_subdistricts_extract.geojson: 4 districts and 5 tehsils from
  the LGD/Bharatmaps GeoParquet files, coverage-simplified to 0.002 degrees to stay small
- lgd_today_extract.json: the matching rows of the LGD daily snapshot

Database tests write rows to blindspot_test and delete exactly the rows they created.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.config import get_settings
from blindspot.db import SessionLocal, init_db, session_scope
from blindspot.geo import admin_for_points, nearest_place
from blindspot.http import FetchError, reset_cooldowns
from blindspot.jobs import boundaries as job
from blindspot.sources import boundaries as src

FIXTURES = Path(__file__).parent / "fixtures" / "boundaries"
AOI = (73.8, 27.6, 77.7, 32.6)
TEST_PLACE_SOURCE = "test:boundaries"


# --- parsers on recorded responses -----------------------------------------------------------


def test_village_display_name_drops_the_hadbast_number() -> None:
    assert src.village_display_name("Bugar (82)") == "Bugar"
    assert src.village_display_name("Kakar (3)") == "Kakar"
    assert src.village_display_name("Raipur Khurd") == "Raipur Khurd"
    assert src.village_display_name("(12)") == "(12)"  # nothing left: keep what the census says


def test_parse_overpass_reads_nodes_and_way_centres() -> None:
    payload = json.loads((FIXTURES / "overpass_in_ch.json").read_text(encoding="utf-8"))
    places = src.parse_overpass(payload)
    assert len(places) == len(payload["elements"]) == 17
    city = next(p for p in places if p.place == "city")
    assert city.name == "Chandigarh" and city.osm_id.startswith("node/")
    way = next(p for p in places if p.osm_id.startswith("way/"))
    assert 30.6 < way.lat < 30.8 and 76.6 < way.lon < 76.9  # from the way's `center`
    assert all(set(p.tags) <= src._KEPT_TAGS for p in places)


def test_overpass_query_selects_the_state_area() -> None:
    query = src.overpass_query("IN-PB")
    assert 'area["ISO3166-2"="IN-PB"]["admin_level"="4"]' in query
    assert "out center tags qt;" in query


def test_parse_geonames_filters_country_kind_population_and_box() -> None:
    text_rows = (FIXTURES / "geonames_cities15000_excerpt.txt").read_text(encoding="utf-8")
    cities = {c.name: c for c in src.parse_geonames_cities(text_rows, AOI, 250_000)}
    assert {"Delhi", "New Delhi", "Ludhiana", "Gurugram", "Noida", "Sonipat"} <= set(cities)
    assert "Karol Bagh" not in cities  # PPLX: a section of Delhi, not a city
    assert "Jind" not in cities  # 167,592 people: under the threshold
    assert "Lahore" not in cities  # Pakistan
    assert "Jaipur" not in cities  # outside the box
    assert cities["Sonipat"].population == 289_333  # ASCII name ("Sonīpat" in GeoNames)


def test_parse_wikidata_keeps_one_city_per_geonames_id() -> None:
    path = FIXTURES / "wikidata_cities.json"
    if not path.exists():
        pytest.skip("no recorded Wikidata answer")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cities = src.parse_wikidata_cities(payload)
    assert cities, "the recorded answer matched no city"
    assert all(c.qid.startswith("Q") for c in cities.values())
    ids = {int(b["gn"]["value"]) for b in payload["results"]["bindings"]}
    assert set(cities) == ids


def test_wikidata_refusal_falls_back_to_the_last_good_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When WDQS refuses (it rate-limits with 429), the last answer on disk is used and cited by
    its own fetch id; with nothing on disk the error propagates."""
    path = FIXTURES / "wikidata_cities.json"
    if not path.exists():
        pytest.skip("no recorded Wikidata answer")
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)

    def refuse(_ids: object) -> object:
        raise FetchError("wikidata", "HTTP 429: too many requests", status_code=429)

    monkeypatch.setattr(src, "_fetch_wikidata_cities", refuse)
    with pytest.raises(FetchError):
        src.fetch_wikidata_cities([1273294])
    saved = {"fetch_id": 2929, "payload": json.loads(path.read_text(encoding="utf-8"))}
    (tmp_path / "boundaries").mkdir(exist_ok=True)
    (tmp_path / "boundaries" / "wikidata_cities.json").write_text(json.dumps(saved))
    result = src.fetch_wikidata_cities([1273294])
    assert result.fetch_id == 2929 and result.cached_error and "429" in result.cached_error
    assert result.by_geonames_id[1273294].qid == "Q1353"  # Delhi


@pytest.mark.parametrize("covering", [True, False])
def test_read_geoparquet_filters_by_box_and_state(tmp_path: Path, covering: bool) -> None:
    """The streaming reader keeps rows whose bbox touches ours and whose code matches, with or
    without the GeoParquet 1.1 bbox covering column."""
    frame = gpd.read_file(FIXTURES / "lgd_subdistricts_extract.geojson")
    path = tmp_path / "subdistricts.parquet"
    frame.to_parquet(path, write_covering_bbox=covering)
    around_hansi = (75.8, 29.0, 76.3, 29.4)
    got = src.read_geoparquet(path, ["subdt_lgd", "state_lgd"], around_hansi, ("state_lgd", [6]),
                              batch_size=2)
    assert set(got["subdt_lgd"]) >= {395, 396}  # Narnaund, Hansi
    assert 271 not in set(got["subdt_lgd"])  # Sangrur: Punjab and far away
    everything = src.read_geoparquet(path, ["subdt_lgd"])
    assert len(everything) == len(frame) and everything.crs.to_epsg() == 4326


# --- areas, Hansi and lookups on real (simplified) polygons -----------------------------------


def _lgd_extract() -> src.LgdToday:
    raw = json.loads((FIXTURES / "lgd_today_extract.json").read_text(encoding="utf-8"))
    states = {
        int(r["state_code"]): src.LgdUnit(int(r["state_code"]), r["state_name"], None,
                                          r["state_or_ut"] == "U")
        for r in raw["states"]
    }
    districts = {
        int(r["district_code"]): src.LgdUnit(int(r["district_code"]), r["district_name"],
                                             int(r["state_code"]))
        for r in raw["districts"]
    }
    subdistricts = {
        int(r["subdistrict_code"]): src.LgdUnit(int(r["subdistrict_code"]),
                                                r["subdistrict_name"], int(r["district_code"]))
        for r in raw["subdistricts"]
    }
    return src.LgdToday(raw["observed_on"], states, districts, subdistricts, (), "fixture")


@pytest.fixture
def areas() -> Iterator[list[int]]:
    """The fixture districts and tehsils loaded through the job's own builders, Hansi derived.
    Yields the ids of every row created; deletes exactly those rows afterwards."""
    init_db()
    lgd = _lgd_extract()
    states = {3: "Punjab", 6: "Haryana"}
    district_frame = gpd.read_file(FIXTURES / "lgd_districts_extract.geojson")
    sub_frame = gpd.read_file(FIXTURES / "lgd_subdistricts_extract.geojson")
    built = [
        *job._district_areas(district_frame, lgd, states, None),
        *job._subdistrict_areas(sub_frame, lgd, states, None),
    ]
    created: list[int] = []
    try:
        with session_scope() as session:
            before = session.execute(text("SELECT COALESCE(max(id), 0) FROM boundary")).scalar()
            job._insert_areas(session, built)
            derived = job._derive_district(session, job.DERIVED_DISTRICTS[0], lgd, "test")
            created = list(
                session.execute(
                    text("SELECT id FROM boundary WHERE id > :b AND source = ANY(:s)"),
                    {"b": before, "s": job.AREA_SOURCES},
                ).scalars()
            )
        assert derived == {"code": 792, "name": "Hansi", "carved_from": ["Hisar"]}
        yield created
    finally:
        if created:
            with session_scope() as session:
                session.execute(text("DELETE FROM boundary WHERE id = ANY(:ids)"), {"ids": created})


def test_area_builders_use_todays_lgd_names_and_parents() -> None:
    lgd = _lgd_extract()
    states = {3: "Punjab", 6: "Haryana"}
    tehsils = job._subdistrict_areas(
        gpd.read_file(FIXTURES / "lgd_subdistricts_extract.geojson"), lgd, states, None
    )
    by_code = {a.code: a for a in tehsils}
    # LGD moved Narnaund and Hansi tehsils from Hisar to the new Hansi district (Dec 2025).
    assert by_code["395"].district == "Hansi" and by_code["396"].district == "Hansi"
    assert by_code["394"].district == "Hisar"
    assert by_code["271"].state == "Punjab" and by_code["271"].district == "Sangrur"


def test_hansi_is_rebuilt_and_cut_out_of_hisar(areas: list[int]) -> None:
    with SessionLocal() as session:
        rows = dict(
            session.execute(
                text(
                    "SELECT name, ST_Area(geom::geography) / 1e6 FROM boundary "
                    "WHERE id = ANY(:ids) AND level = 'district'"
                ),
                {"ids": areas},
            ).all()
        )
    # Full-detail values (docs/research/geography-and-basemaps.md): the Hansi + Narnaund tehsil
    # union is 1,319 km2, and Hisar had 4,074 km2 before the split. Fixtures are simplified.
    assert rows["Hansi"] == pytest.approx(1319, rel=0.03)
    assert rows["Hisar"] == pytest.approx(4074 - 1319, rel=0.03)
    assert rows["Bhiwani"] == pytest.approx(3291, rel=0.03)  # touches Hansi; not cut


def test_points_get_todays_district_and_tehsil(areas: list[int]) -> None:
    points = {
        "Sangrur": (75.84, 30.25),  # the task's check
        "Narnaund": (76.14, 29.22),  # now in Hansi district
        "Hisar city": (75.7229, 29.1539),
        "Lahore": (74.34, 31.55),  # Pakistan: no Indian polygon
    }
    with SessionLocal() as session:
        found = dict(zip(points, admin_for_points(session, list(points.values())), strict=True))
    assert found["Sangrur"] is not None
    assert (found["Sangrur"].state, found["Sangrur"].district) == ("Punjab", "Sangrur")
    assert found["Sangrur"].subdistrict == "Sangrur"
    assert found["Narnaund"] is not None and found["Narnaund"].district == "Hansi"
    assert found["Narnaund"].subdistrict == "Narnaund"
    assert found["Hisar city"] is not None and found["Hisar city"].district == "Hisar"
    assert found["Lahore"] is None


# --- routes ------------------------------------------------------------------------------------


@pytest.fixture
def village() -> Iterator[int]:
    """One real Census 2011 village point (Bugar (82), Sangrur; from the research doc's sample)."""
    with session_scope() as session:
        place_id = session.execute(
            text(
                "INSERT INTO place (kind, name, lat, lon, geom, population, source, props) VALUES "
                "('village', 'Bugar', 30.21497, 75.59171, "
                "ST_SetSRID(ST_MakePoint(75.59171, 30.21497), 4326), 1232, :s, "
                "'{\"census_name\": \"Bugar (82)\"}') RETURNING id"
            ),
            {"s": TEST_PLACE_SOURCE},
        ).scalar_one()
    try:
        yield place_id
    finally:
        with session_scope() as session:
            session.execute(text("DELETE FROM place WHERE id = :id"), {"id": place_id})


@pytest.fixture
def client() -> Iterator[TestClient]:
    from blindspot.api.main import app

    with TestClient(app) as test_client:
        yield test_client


def _assert_envelope(body: dict) -> None:
    assert body["generated_at"].endswith("Z")
    assert isinstance(body["sources"], list)
    for ref in body["sources"]:
        assert {"key", "label", "url", "fetched_at", "fetch_id"} <= set(ref)


def test_boundaries_route_returns_simplified_geojson(client: TestClient, areas: list[int]) -> None:
    response = client.get(
        "/api/geo/boundaries",
        params={"level": "district", "state": "Haryana", "bbox": "74.5,28.3,76.6,30.6"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_envelope(body)
    fc = body["features"]
    assert fc["type"] == "FeatureCollection" and body["count"] == len(fc["features"])
    by_name = {f["properties"]["name"]: f for f in fc["features"]}
    assert {"Hansi", "Hisar", "Bhiwani", "Jind"} <= set(by_name)
    assert "Sangrur" not in by_name  # Punjab, filtered out by state
    hansi = by_name["Hansi"]
    assert hansi["geometry"]["type"] == "MultiPolygon"
    assert hansi["properties"]["code"] == "792" and hansi["properties"]["derived"]
    assert any(note.startswith("Hansi (LGD 792)") for note in body["notes"])
    # The same state by ISO code and by LGD code.
    for alias in ("IN-HR", "6"):
        again = client.get("/api/geo/boundaries", params={"level": "district", "state": alias})
        assert again.status_code == 200


def test_lookup_route_names_district_and_nearest_village(
    client: TestClient, areas: list[int], village: int
) -> None:
    response = client.get("/api/geo/lookup", params={"lat": 30.22, "lon": 75.60})
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_envelope(body)
    assert (body["state"], body["district"]) == ("Punjab", "Sangrur")
    assert body["nearest_place"]["name"] == "Bugar"
    assert body["nearest_place"]["distance_km"] < 2


def test_places_and_cities_routes(client: TestClient, village: int) -> None:
    found = client.get("/api/geo/places", params={"q": "Buga"})
    assert found.status_code == 200
    _assert_envelope(found.json())
    assert any(p["name"] == "Bugar" for p in found.json()["places"])
    cities = client.get("/api/geo/cities")
    assert cities.status_code == 200
    body = cities.json()
    _assert_envelope(body)
    assert body["count"] == len(body["cities"])


def test_tiles_route_serves_vector_tiles(client: TestClient, areas: list[int]) -> None:
    tile = client.get("/api/geo/tiles/7/90/52.pbf")  # covers Sangrur
    assert tile.status_code == 200
    assert tile.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert b"districts" in tile.content  # the layer name is in the protobuf
    assert client.get("/api/geo/tiles/7/0/0.pbf").status_code == 204  # Arctic: nothing there
    assert client.get("/api/geo/tiles/3/9/0.pbf").status_code == 404  # outside the z3 grid


def test_bad_parameters_are_rejected(client: TestClient) -> None:
    assert client.get("/api/geo/boundaries", params={"level": "village"}).status_code == 422
    assert client.get("/api/geo/boundaries", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get("/api/geo/lookup", params={"lat": 95, "lon": 75}).status_code == 422


def test_nearest_place_is_none_without_places_nearby() -> None:
    with SessionLocal() as session:
        assert nearest_place(session, -120.0, -60.0, max_km=1) is None  # Southern Ocean


# --- conditional download (network) -------------------------------------------------------------


def test_download_sends_etag_and_reuses_the_cached_copy(tmp_path: Path, monkeypatch) -> None:
    """Second download of an unchanged release file is a 304 served from the cache."""
    init_db()
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    reset_cooldowns()
    url = f"{src.RELEASES}/states/SOI_States.coverage.png"  # 3.8 KB, part of the same release
    try:
        first = src.download("boundaries", url)
        second = src.download("boundaries", url)
    except FetchError as exc:
        pytest.skip(f"GitHub not reachable: {exc}")
    assert first.path.exists() and first.size > 0 and not first.not_modified
    assert second.not_modified and second.sha256 == first.sha256
    assert second.path == first.path and second.fetch_id != first.fetch_id
