"""Connectors for administrative boundaries and named places: fetch and parse, no database writes.

Where each layer comes from, and why (docs/research/geography-and-basemaps.md):

- States: Survey of India (SOI) polygons. For political maps of India "SoI published maps or SoI
  digital boundary data are the standard to be used" (DST Geospatial Guidelines, 15 Feb 2021,
  para xiii), so these are the outlines we draw and clip counts with.
- Districts and subdistricts: NIC Bharatmaps polygons keyed by Local Government Directory (LGD)
  codes (Dec 2023): the only open polygons with LGD codes that include Malerkotla (2021).
- Names: today's LGD list (a daily snapshot of lgdirectory.gov.in), so every area carries the name
  LGD uses today, and districts created after 2023 are noticed instead of silently missed.
- Villages: Census 2011 village points (NIC Bharatmaps): every village, with its population.
- Towns: OpenStreetMap through the Overpass API (the census village list has no towns).
- Receptor cities: GeoNames cities15000 (CC BY 4.0), cross-referenced on Wikidata.

The polygon and village files come from github.com/ramSeraph/indian_admin_boundaries (CC0; credit
SOI, NIC Bharatmaps / LGD and DataMeet). They are fetched with a conditional GET, so a weekly run
downloads a file again only when it has changed upstream.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import shapely

from blindspot.config import BBox, get_settings
from blindspot.http import USER_AGENT, FetchError, fetch, set_fetch_records
from blindspot.provenance import SourceInfo, register_source, track_fetch

if TYPE_CHECKING:  # geopandas and pyarrow are imported inside the job-time readers below, so the
    # web API (the geo router imports this module for its source labels) starts without them.
    import geopandas as gpd
    import pyarrow as pa

log = logging.getLogger(__name__)

WEEK_MINUTES = 7 * 24 * 60

for _info in (
    SourceInfo(
        "boundaries",
        "Survey of India states + LGD/Bharatmaps districts (DataMeet / ramSeraph mirror)",
        "boundaries",
        WEEK_MINUTES,
        "https://github.com/ramSeraph/indian_admin_boundaries",
    ),
    SourceInfo(
        "lgd",
        "Local Government Directory names and codes (daily snapshot)",
        "boundaries",
        WEEK_MINUTES,
        "https://ramseraph.github.io/opendata/lgd/",
    ),
    SourceInfo(
        "census2011",
        "Census 2011 village points (NIC Bharatmaps, ramSeraph mirror)",
        "places",
        WEEK_MINUTES,
        "https://github.com/ramSeraph/indian_admin_boundaries/releases/tag/census-2011",
    ),
    SourceInfo(
        "osm",
        "OpenStreetMap places via the Overpass API",
        "places",
        WEEK_MINUTES,
        "https://wiki.openstreetmap.org/wiki/Overpass_API",
    ),
    SourceInfo(
        "geonames",
        "GeoNames cities15000 (CC BY 4.0)",
        "places",
        WEEK_MINUTES,
        "https://download.geonames.org/export/dump/readme.txt",
    ),
    SourceInfo(
        "wikidata",
        "Wikidata Query Service (city populations)",
        "places",
        WEEK_MINUTES,
        "https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service",
    ),
):
    register_source(_info)

RELEASES = "https://github.com/ramSeraph/indian_admin_boundaries/releases/download"
SOI_STATES_URL = f"{RELEASES}/states/SOI_States.parquet"
LGD_DISTRICTS_URL = f"{RELEASES}/districts/LGD_Districts.parquet"
LGD_SUBDISTRICTS_URL = f"{RELEASES}/subdistricts/LGD_Subdistricts.parquet"
CENSUS_VILLAGES_URL = f"{RELEASES}/census-2011/Census_Villages.parquet"
LGD_INDEX_URL = "https://ramseraph.github.io/opendata/lgd/data/metadata.json"
GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
# The public Overpass instances listed on the OSM wiki; the main one is often "at capacity", so a
# failed query moves on to the next (fetch() pauses the "osm" source after 3 failed calls in a row).
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# The states/UTs we watch: LGD state code -> ISO 3166-2 code (the OSM area tag).
# Codes from lgdirectory.gov.in and ISO 3166-2:IN.
AOI_STATES: dict[int, str] = {3: "IN-PB", 6: "IN-HR", 7: "IN-DL", 4: "IN-CH"}

# Overpass runs its query for up to 180 s server-side; give the HTTP read a little longer.
OVERPASS_TIMEOUT = httpx.Timeout(240.0, connect=15.0)
# Big release files: no read stall longer than 60 s, but no cap on the whole download.
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


# --- downloads with a local cache ---------------------------------------------------------------


@dataclass(frozen=True)
class CachedFile:
    """A dataset file in the local cache, and the fetch that confirmed it is current."""

    path: Path
    url: str
    sha256: str
    size: int
    fetch_id: int | None
    fetched_at: datetime | None
    not_modified: bool  # True when the server answered 304: our cached copy is still current


def download(source_key: str, url: str) -> CachedFile:
    """GET a dataset file into CACHE_DIR/boundaries, sending the cached copy's ETag.

    An unchanged file comes back as HTTP 304 with no body, so the weekly run costs one small
    request per file. Either way the call is one source_fetch row (via fetch()). Raises FetchError
    when the download fails; the caller then keeps the data it loaded last time.
    """
    settings = get_settings()
    path = settings.cache_path("boundaries", url.rsplit("/", 1)[-1])
    sidecar = path.with_name(path.name + ".json")
    cached: dict[str, Any] | None = None
    if path.exists() and sidecar.exists():
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    headers = {"If-None-Match": cached["etag"]} if cached and cached.get("etag") else None
    result = fetch(
        source_key,
        url,
        expect="bytes",
        headers=headers,
        timeout=DOWNLOAD_TIMEOUT,
        meta={"file": path.name},
    )
    if result.status_code == 304:
        if cached is None:  # only possible if the server ignores the protocol; never trust it
            raise FetchError(source_key, "HTTP 304 without a cached copy", fetch_id=result.fetch_id)
        set_fetch_records(result.fetch_id, 0, {"not_modified": True, "sha256": cached["sha256"]})
        return CachedFile(
            path, url, cached["sha256"], cached["size"], result.fetch_id, result.fetched_at, True
        )
    body: bytes = result.data
    digest = hashlib.sha256(body).hexdigest()
    part = path.with_name(path.name + ".part")
    part.write_bytes(body)
    part.replace(path)  # atomic: a crash mid-write never leaves a half file under the real name
    sidecar.write_text(
        json.dumps(
            {
                "url": url,
                "etag": result.headers.get("etag"),
                "last_modified": result.headers.get("last-modified"),
                "sha256": digest,
                "size": len(body),
                "fetch_id": result.fetch_id,
            }
        ),
        encoding="utf-8",
    )
    set_fetch_records(result.fetch_id, 0, {"sha256": digest, "bytes": len(body)})
    return CachedFile(path, url, digest, len(body), result.fetch_id, result.fetched_at, False)


# --- today's LGD names and codes ----------------------------------------------------------------


@dataclass(frozen=True)
class LgdUnit:
    code: int
    name: str
    parent: int | None  # the state of a district, the district of a subdistrict
    is_ut: bool = False  # states only: a union territory (Delhi, Chandigarh, J&K, Ladakh, ...)


@dataclass(frozen=True)
class LgdToday:
    """States, districts and subdistricts as the Local Government Directory lists them today."""

    observed_on: str  # the day the snapshot of lgdirectory.gov.in was taken
    states: dict[int, LgdUnit]
    districts: dict[int, LgdUnit]
    subdistricts: dict[int, LgdUnit]
    fetch_ids: tuple[int | None, ...]
    sha256: str  # of the three files together: changes only when LGD's lists change

    def district_name(self, code: int) -> str | None:
        unit = self.districts.get(code)
        return unit.name if unit else None

    def subdistricts_of(self, district: int) -> list[LgdUnit]:
        return [u for u in self.subdistricts.values() if u.parent == district]


def fetch_lgd_today() -> LgdToday:
    """Today's LGD lists from the daily snapshot (index -> three small parquet files).

    The index publishes a sha256 for every file; a file whose hash does not match is rejected
    rather than half-trusted.
    """
    import pyarrow.parquet as pq

    index = fetch("lgd", LGD_INDEX_URL)
    datasets = index.data["datasets"]
    fetch_ids: list[int | None] = [index.fetch_id]
    tables: dict[str, list[dict[str, Any]]] = {}
    observed_on = ""
    combined = hashlib.sha256()
    for name in ("states", "districts", "subdistricts"):
        daily = datasets[name]["daily"]
        meta = daily["file"]
        result = fetch("lgd", meta["url"], expect="bytes", meta={"dataset": name})
        if hashlib.sha256(result.data).hexdigest() != meta["sha256"]:
            raise ValueError(f"LGD {name} snapshot does not match its published sha256")
        combined.update(meta["sha256"].encode())
        rows = pq.read_table(io.BytesIO(result.data)).to_pylist()
        set_fetch_records(result.fetch_id, len(rows), {"observed_on": daily["observed_on"]})
        tables[name] = rows
        fetch_ids.append(result.fetch_id)
        observed_on = max(observed_on, str(daily["observed_on"]))
    set_fetch_records(index.fetch_id, len(datasets))
    states = {
        int(r["state_code"]): LgdUnit(
            int(r["state_code"]), r["state_name"].strip(), None, r["state_or_ut"] == "U"
        )
        for r in tables["states"]
    }
    districts = {
        int(r["district_code"]): LgdUnit(
            int(r["district_code"]), r["district_name"].strip(), int(r["state_code"])
        )
        for r in tables["districts"]
    }
    subdistricts = {
        int(r["subdistrict_code"]): LgdUnit(
            int(r["subdistrict_code"]), r["subdistrict_name"].strip(), int(r["district_code"])
        )
        for r in tables["subdistricts"]
    }
    return LgdToday(
        observed_on, states, districts, subdistricts, tuple(fetch_ids), combined.hexdigest()
    )


# --- polygon and village files ------------------------------------------------------------------


def read_geoparquet(
    path: Path,
    columns: Sequence[str],
    bbox: BBox | None = None,
    where: tuple[str, Iterable[int]] | None = None,
    batch_size: int = 256,
) -> gpd.GeoDataFrame:
    """Rows of a GeoParquet file whose bounding box touches `bbox` and whose `where` column is
    one of the given values, read in small batches.

    The LGD files are each one row group, so a filtered read would still decode the whole file at
    once (an 800 MB peak for these four files); in batches, memory holds one batch plus the rows
    kept. Rows are matched on the file's `bbox` covering column (GeoParquet 1.1).
    """
    import geopandas as gpd
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    use_bbox = bbox is not None and "bbox" in parquet.schema_arrow.names
    wanted = [*columns, "geometry"]
    read = wanted + ["bbox"] if use_bbox else wanted
    kept = []
    for batch in parquet.iter_batches(batch_size=batch_size, columns=read):
        mask = _bbox_touches(batch.column("bbox"), bbox) if use_bbox and bbox else None
        if where is not None:
            column = batch.column(where[0])
            match = pc.is_in(column, pa.array(list(where[1]), type=column.type))
            mask = match if mask is None else pc.and_(mask, match)
        rows = batch if mask is None else batch.filter(mask)
        if rows.num_rows:
            kept.append(rows.select(wanted))
    schema = pa.schema([parquet.schema_arrow.field(c) for c in wanted])
    table = pa.Table.from_batches(kept, schema=schema)
    geometry = gpd.GeoSeries.from_wkb(table.column("geometry").to_numpy(zero_copy_only=False))
    frame = gpd.GeoDataFrame(
        table.drop_columns(["geometry"]).to_pandas(), geometry=geometry.values, crs="EPSG:4326"
    )
    if bbox is not None and not use_bbox:  # no covering column: filter on the geometries
        frame = frame[frame.intersects(shapely.box(*bbox))]
    return frame


def _bbox_touches(boxes: pa.StructArray, bbox: BBox) -> pa.BooleanArray:
    """Which rows' bounding boxes overlap `bbox` (min_lon, min_lat, max_lon, max_lat)."""
    import pyarrow.compute as pc

    min_lon, min_lat, max_lon, max_lat = bbox
    return pc.and_(
        pc.and_(
            pc.less_equal(pc.struct_field(boxes, "xmin"), max_lon),
            pc.greater_equal(pc.struct_field(boxes, "xmax"), min_lon),
        ),
        pc.and_(
            pc.less_equal(pc.struct_field(boxes, "ymin"), max_lat),
            pc.greater_equal(pc.struct_field(boxes, "ymax"), min_lat),
        ),
    )


def read_soi_states(path: Path) -> gpd.GeoDataFrame:
    """All 40 SOI polygons: 36 states/UTs plus 4 inter-state disputed areas (State_LGD 0).

    The whole country, not just our area: the map draws India's outline from these.
    STATE has diacritics (HARYĀNA); STATE_C is the plain-ASCII form.
    """
    return read_geoparquet(path, ["STATE_C", "State_LGD"])


def read_lgd_districts(path: Path, bbox: BBox) -> gpd.GeoDataFrame:
    """LGD district polygons touching `bbox`, whole (not clipped)."""
    return read_geoparquet(path, ["dist_lgd", "dtname", "state_lgd", "dtcode11", "year_stat"], bbox)


def read_lgd_subdistricts(path: Path, bbox: BBox, states: Iterable[int]) -> gpd.GeoDataFrame:
    """LGD subdistrict (tehsil) polygons of `states` touching `bbox`."""
    columns = ["subdt_lgd", "sdtname", "dist_lgd", "dtname", "state_lgd", "sdtcode11"]
    return read_geoparquet(path, columns, bbox, ("state_lgd", states))


def read_census_villages(path: Path, bbox: BBox, states: Iterable[int]) -> gpd.GeoDataFrame:
    """Census 2011 village points of `states`, with 2011 population and census codes."""
    columns = [
        "vilname", "vilcode11", "t_pop2011", "ST_LGD", "DT_LGD", "SDT_LGD", "dtname",
        "GP_NAME", "Remarks",
    ]
    return read_geoparquet(path, columns, bbox, ("ST_LGD", states))


_HADBAST = re.compile(r"\s*\(\s*\d+\s*\)\s*$")


def village_display_name(census_name: str) -> str:
    """'Bugar (82)' -> 'Bugar'. Punjab and Haryana census names end in the village's hadbast
    (revenue record) number, which tells two same-named villages apart but is not how anyone
    says the name. The full census name is kept alongside."""
    return _HADBAST.sub("", census_name).strip() or census_name.strip()


# --- OpenStreetMap towns and villages -----------------------------------------------------------


@dataclass(frozen=True)
class OsmPlace:
    osm_id: str  # "node/123", "way/456", "relation/789"
    place: str  # OSM place=* value: city, town, village or hamlet
    name: str
    lat: float
    lon: float
    population: int | None
    tags: dict[str, str]


def overpass_query(iso_code: str) -> str:
    """Every named city, town, village and hamlet inside one state's OSM boundary."""
    return (
        "[out:json][timeout:180];"
        f'area["ISO3166-2"="{iso_code}"]["admin_level"="4"]->.a;'
        'nwr["place"~"^(city|town|village|hamlet)$"]["name"](area.a);'
        "out center tags qt;"
    )


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    digits = value.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def parse_overpass(payload: dict[str, Any]) -> list[OsmPlace]:
    """Overpass JSON -> places. Ways and relations come with a `center`; nodes with lat/lon."""
    places = []
    for element in payload.get("elements", []):
        tags = element.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        where = element.get("center") or element
        if not name or "lat" not in where:
            continue
        places.append(
            OsmPlace(
                osm_id=f"{element['type']}/{element['id']}",
                place=tags.get("place", ""),
                name=name,
                lat=float(where["lat"]),
                lon=float(where["lon"]),
                population=_int_or_none(tags.get("population")),
                tags={k: v for k, v in tags.items() if k in _KEPT_TAGS},
            )
        )
    return places


_KEPT_TAGS = {"name", "name:en", "name:hi", "name:pa", "place", "population", "wikidata"}


@dataclass(frozen=True)
class OsmResult:
    places: list[OsmPlace]
    fetch_id: int | None
    endpoint: str
    osm_base: str | None  # the OSM data timestamp the server answered from


def fetch_osm_places(iso_code: str) -> OsmResult:
    """Run the places query for one state, trying each Overpass instance until one answers."""
    query = overpass_query(iso_code)
    errors: list[str] = []
    for endpoint in OVERPASS_URLS:
        try:
            result = fetch(
                "osm",
                endpoint,
                method="POST",
                data={"data": query},
                timeout=OVERPASS_TIMEOUT,
                meta={"area": iso_code},
            )
        except FetchError as exc:
            errors.append(str(exc))
            if exc.status_code is not None and exc.status_code < 500 and exc.status_code != 429:
                raise  # the query itself is wrong; another server will not fix it
            continue
        places = parse_overpass(result.data)
        osm_base = (result.data.get("osm3s") or {}).get("timestamp_osm_base")
        set_fetch_records(result.fetch_id, len(places), {"osm_base": osm_base})
        return OsmResult(places, result.fetch_id, endpoint, osm_base)
    raise FetchError("osm", f"every Overpass instance failed for {iso_code}: {errors}")


# --- receptor cities: GeoNames + Wikidata -------------------------------------------------------


@dataclass(frozen=True)
class GeoNamesCity:
    geonames_id: int
    name: str  # ASCII name: "Sonipat", not "Sonīpat"
    lat: float
    lon: float
    feature_code: str  # PPLC capital, PPLA state seat, PPLA2 district seat, PPL ...
    population: int
    alternate_names: tuple[str, ...]
    modified: str  # the date GeoNames last edited the row


@dataclass(frozen=True)
class GeoNamesResult:
    cities: list[GeoNamesCity]
    fetch_id: int | None


def fetch_geonames_cities(bbox: BBox, min_population: int, country: str = "IN") -> GeoNamesResult:
    """Populated places of one country inside `bbox` with at least `min_population` people."""
    result = fetch("geonames", GEONAMES_CITIES_URL, expect="bytes", timeout=DOWNLOAD_TIMEOUT)
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        text = archive.read("cities15000.txt").decode("utf-8")
    cities = parse_geonames_cities(text, bbox, min_population, country)
    set_fetch_records(result.fetch_id, len(cities), {"bbox": list(bbox), "min_pop": min_population})
    return GeoNamesResult(cities, result.fetch_id)


def parse_geonames_cities(
    text: str, bbox: BBox, min_population: int, country: str = "IN"
) -> list[GeoNamesCity]:
    """Rows of the GeoNames cities table (tab-separated) that pass the filters.

    Sections of a populated place (feature code PPLX, e.g. Karol Bagh in Delhi) are left out: a
    receptor is a whole city.
    """
    cities = []
    min_lon, min_lat, max_lon, max_lat = bbox
    # Columns: geonames.org/export/dump/readme.txt ("The main 'geoname' table").
    for row in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(row) < 19 or row[8] != country or row[6] != "P" or row[7] == "PPLX":
            continue
        lat, lon, population = float(row[4]), float(row[5]), int(row[14] or 0)
        if population < min_population:
            continue
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        cities.append(
            GeoNamesCity(
                geonames_id=int(row[0]),
                name=row[2],
                lat=lat,
                lon=lon,
                feature_code=row[7],
                population=population,
                alternate_names=tuple(a for a in row[3].split(",") if a),
                modified=row[18],
            )
        )
    return cities


@dataclass(frozen=True)
class WikidataCity:
    qid: str
    label: str | None
    population: int | None
    population_date: str | None  # YYYY-MM-DD of the best-ranked value, when Wikidata gives one


@dataclass(frozen=True)
class WikidataResult:
    by_geonames_id: dict[int, WikidataCity]
    fetch_id: int | None  # the fetch the answer came from (an earlier one when `cached_error`)
    cached_error: str | None = None  # set when WDQS refused today and the last answer was used


def fetch_wikidata_cities(geonames_ids: Sequence[int]) -> WikidataResult:
    """Wikidata items for GeoNames ids (property P1566), with their best-ranked population.

    Joining on the GeoNames id avoids matching cities by name (Wikidata has a "Chandigarh"
    village in West Bengal, for one). A city with several best-ranked values keeps the newest.
    WDQS rate-limits hard at times (429), so each good answer is kept on disk with its fetch id;
    when WDQS refuses, that last answer is used and cited by its own fetch, rather than letting
    the choice of receptor cities flip with Wikidata's mood.
    """
    cache = get_settings().cache_path("boundaries", "wikidata_cities.json")
    try:
        result = _fetch_wikidata_cities(geonames_ids)
    except FetchError as exc:
        if not cache.exists():
            raise
        saved = json.loads(cache.read_text(encoding="utf-8"))
        cities = parse_wikidata_cities(saved["payload"])
        return WikidataResult(cities, saved.get("fetch_id"), cached_error=str(exc)[:300])
    cache.write_text(
        json.dumps({"fetch_id": result[1], "payload": result[0]}), encoding="utf-8"
    )
    cities = parse_wikidata_cities(result[0])
    set_fetch_records(result[1], len(cities))
    return WikidataResult(cities, result[1])


def _fetch_wikidata_cities(geonames_ids: Sequence[int]) -> tuple[dict[str, Any], int | None]:
    values = " ".join(f'"{gid}"' for gid in geonames_ids)
    query = f"""
        SELECT ?gn ?item ?itemLabel ?pop ?popDate WHERE {{
          VALUES ?gn {{ {values} }}
          ?item wdt:P1566 ?gn .
          OPTIONAL {{ ?item p:P1082 ?ps . ?ps ps:P1082 ?pop ; a wikibase:BestRank .
                      OPTIONAL {{ ?ps pq:P585 ?popDate }} }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}"""
    return _wikidata_query(query)


def _wikidata_query(query: str) -> tuple[dict[str, Any], int | None]:
    """One SPARQL GET to the Wikidata Query Service, recorded in source_fetch.

    Not through blindspot.http.fetch: WDQS's bot filter answers 403 to httpx however the request
    is dressed (same User-Agent and headers get 200 from curl, urllib and requests; checked
    2026-10-30), so this one call uses the standard library and track_fetch. One attempt per
    run, no retries: WDQS asks clients to back off, and the cities step works without it.
    """
    url = f"{WIKIDATA_SPARQL_URL}?{urllib.parse.urlencode({'query': query})}"
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    )
    with track_fetch("wikidata", url=url) as tracker:
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                tracker.http_status = response.status
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            tracker.http_status = exc.code
            body = exc.read(300).decode("utf-8", "replace")
            raise FetchError("wikidata", f"HTTP {exc.code}: {body}", status_code=exc.code) from None
        except OSError as exc:  # DNS failure, refused connection, timeout (URLError is an OSError)
            raise FetchError("wikidata", f"{type(exc).__name__}: {exc}") from None
        tracker.records = len(payload["results"]["bindings"])
    return payload, tracker.fetch_id


def parse_wikidata_cities(payload: dict[str, Any]) -> dict[int, WikidataCity]:
    """SPARQL JSON results -> {GeoNames id: Wikidata city}."""
    best: dict[int, WikidataCity] = {}
    for binding in payload["results"]["bindings"]:
        gid = int(binding["gn"]["value"])
        pop = binding.get("pop", {}).get("value")
        city = WikidataCity(
            qid=binding["item"]["value"].rsplit("/", 1)[-1],
            label=binding.get("itemLabel", {}).get("value"),
            population=int(float(pop)) if pop else None,
            population_date=(binding.get("popDate", {}).get("value") or "")[:10] or None,
        )
        old = best.get(gid)
        if old is None or _wikidata_rank(city) > _wikidata_rank(old):
            best[gid] = city
    return best


def _wikidata_rank(city: WikidataCity) -> tuple[bool, str, int]:
    """Prefer a value that exists, then the newest date, then the larger number."""
    return (city.population is not None, city.population_date or "", city.population or 0)
