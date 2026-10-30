# Geography & basemaps: India-compliant boundaries, places, basemaps

Scope: Punjab (PB), Haryana (HR), Delhi (DL), Chandigarh (CH) + receptor cities. All calls made 2026-10-30, 21:15-22:10 IST,
from the team's Windows dev box. Tags: **[OK]** verified today with a real call, **[WARN]** works but has a caveat,
**[FAIL]** failed today (error quoted), **UNVERIFIED** could not be tested (reason given).

## 0. Decision (TL;DR)

| Need | Use | Why |
|---|---|---|
| National + state lines drawn on every map | **Survey of India (SOI) state polygons** `SOI_States.parquet` (ramSeraph/indian_admin_boundaries), loaded into PostGIS and served by our API as MVT. Fallback: `https://indianopenmaps.com/states/soi/{z}/{x}/{y}.pbf` | SOI data *is* the legal standard (DST 2021 guidelines, para xiii). Covers all of J&K + Ladakh incl. PoK, Gilgit-Baltistan, Aksai Chin (Ladakh bbox reaches 37.088N / 72.51E) |
| District / tehsil / block / village polygons for analytics | **LGD-coded Bharatmaps polygons** (`LGD_*`, Dec-2023) + **today's LGD code list** (daily snapshot) + **derived Hansi** (sec. 5) | Only polygon set with LGD + Census-2011 codes that includes Malerkotla. All of its codes are still valid in today's LGD list |
| Dark basemap | **OpenFreeMap `dark`**: keyless, no request limits, commercial use allowed. Remove 3 boundary layers and filter 2 region labels (sec. 9) | CARTO now requires an API key (sec. 8.1) |
| Imagery toggle | **EOX Sentinel-2 cloudless 2025** WMTS: keyless, CORS `*` | 10 m imagery with no boundaries baked in. CC BY-NC-SA 4.0, fine for a hackathon |
| Receptor cities | **Wikidata SPARQL** (coordinates + best-rank population), with **GeoNames cities15000** filling missing populations | 18/18 cities matched. Wikidata has no P1082 (population) for Panipat, Karnal or Sonipat |
| Village join targets (PB) | **LGD village polygons** (12,832) + **Census-2011 village points** (12,708, with population). OSM only for extra labels | OSM has 7,882 places against 13,014 LGD villages and almost no codes |

## 1. What "India-compliant" means for judges

DST *Guidelines for acquiring and producing Geospatial Data and Geospatial Data Services including Maps* (15-Feb-2021),
para xiii: "For political Maps of India of any scale including national, state and other boundaries, SoI published maps
or SoI digital boundary data are the standard to be used, which shall be made easily downloadable for free and their
digital display and printing shall be permissible." (Quoted from the ramSeraph/indian_admin_boundaries README. **[FAIL]**
both DST PDF URLs, `https://dst.gov.in/sites/default/files/Final%20Approved%20Guidelines%20on%20Geospatial%20Data_0.pdf`
and the same URL without `_0`, return HTTP 404 today.)

UI checklist:
- The India outline comes from SOI, with no LoC, LAC or "claim" lines drawn as borders.
- No Pakistan-China or Pakistan-Afghanistan line runs through Gilgit-Baltistan.
- No "Azad Kashmir" or "Gilgit-Baltistan" region labels.
- The credit reads "Boundaries: Survey of India". Never "certified by SOI".

What the stock basemaps actually encode (decoded live tiles over Kashmir: z5/22/12, z7/90/50, z6/45/25):
- **OpenFreeMap** (OpenMapTiles 3.16), source-layer `boundary`: `disputed=1` lines named `LineofControl`, `LineofActualControl`, `ChineseclaimlineMay2020`, `BorderGilgit-Baltistan-AJK`; China-India claim lines with `claimed_by="IN"`; non-disputed `adm0_l/adm0_r` = `PAK/AFG` and `PAK/CHN` lines in G-B. Source-layer `place` has `class=state` labels **"Azad Kashmir"** and **"Gilgit-Baltistan"**.
- **CARTO** (Protomaps schema 4.15.2), source-layer `boundaries`: `kind` = country, unrecognized_country or region; `kind_detail` = 2, 3 or 4; `disputed=true` on the disputed lines, plus Natural Earth `brk_a3` codes (B04-B09, B45) at z5. Region labels in the tested tiles were Indian only (Punjab, J&K, Ladakh).
- So in both styles: drop every boundary layer and draw SOI lines instead (sec. 9).

## 2. Boundary sources (PB/HR/DL/CH counts measured on the downloaded files)

**A. SOI via ramSeraph releases. [OK] RECOMMENDED for drawing.**
- **URLs:** `https://github.com/ramSeraph/indian_admin_boundaries/releases/download/states/SOI_States.parquet` (19.1 MB, 2.6 s), `.../districts/SOI_Districts.parquet` (46.2 MB, 26 s), `.../subdistricts/SOI_Subdistricts.parquet` (83.4 MB). Each also ships as `.pmtiles` and `.geojsonl.7z`.
- **Format:** GeoParquet 1.1, CRS84, with a `bbox` covering column (bbox pushdown works).
- **Upstream:** SOI `onlinemaps.surveyofindia.gov.in/Digital_Product_Show.aspx`.
- **Licence:** compilation is CC0, with a request to credit SOI and DataMeet. The display permission comes from para xiii above.
- **States:** 40 rows = 36 states/UTs with `State_LGD`, plus 4 inter-state `DISPUTED (...)` polygons (`State_LGD=0`). Keep these when dissolving the India outline.
- **Districts:** 742 rows; PB 22 (**no Malerkotla**), HR 22 (has Charkhi Dadri, still says "MEWAT" for Nuh), DL 11, CH 1. Vintage is roughly 2016-2020. Columns: `District, STATE, State_LGD, DISTRICT_L` (= LGD district code), `STATE_C, District_C` (ASCII).
- **Tehsils:** PB 78, HR 77, DL 35, CH 1. Columns `TEHSIL, TEHSIL_C, District_C, STATE_C` only. **No LGD codes**, so tehsils have to be matched by name.
- **Gotchas:** `STATE`/`District` carry diacritics (`HARYĀNA`, `SANGRŪR`), so filter on the `_C` columns. 2 invalid geometries among the PB/HR/DL/CH districts and 4 among the tehsils.

**B. LGD/Bharatmaps polygons via ramSeraph releases. [OK] RECOMMENDED for analytics.**
- **URLs:** `.../districts/LGD_Districts.parquet` (34.5 MB, 33.6 s), `.../subdistricts/LGD_Subdistricts.parquet` (95 MB), `.../blocks/LGD_Blocks.parquet` (100 MB), `.../villages/LGD_Villages.parquet` (545 MB).
- **Upstream:** NIC Bharatmaps `mapservice.gov.in/gismapservice/rest/services/BharatMapService/...`.
- **Licence:** the compilation is CC0, but the indianopenmaps catalogue files these routes under `/not-so-open/` because the upstream terms are unstated. Credit NIC/Bharatmaps + LGD + DataMeet.
- **Counts (Dec-2023 vintage):** districts PB **23** (Malerkotla `dist_lgd=737`, `year_stat=2022`), HR 22 (`Nuh`=604, `Charki Dadri`=701), DL 11 (incl. Shahdara 671), CH 1, 0 invalid geometries. Subdistricts PB 79, HR 82 rows but 81 codes (**Loharu 401 is split into 2 rows**), DL 34, CH 1, 3 invalid. Blocks PB 153, HR 142, DL 12, CH 1. Villages PB 12,832 polygons (11 invalid).
- **Code columns:** `state_lgd, dist_lgd, subdt_lgd, block_lgd, vil_lgd, gp_code/gp_name`, Census-2011 `stcode11/dtcode11/sdtcode11/vilcode11`.
- **Coverage against today's LGD:** every polygon code is still valid. Today's PB list has 18 subdistricts and 8 blocks without a polygon; HR has 62 subdistricts (mostly new sub-tehsils) and 1 block without one.

**C. LGD daily snapshot (names and codes, no geometry). [OK] Use it to refresh names/codes daily.**
- **Index:** `https://ramseraph.github.io/opendata/lgd/data/metadata.json`. Its `datasets.<name>.daily.file.url` gives a content-hashed parquet URL, e.g. `.../districts.8edd1bf95e5bfeec.parquet`. `published_at 2026-10-30T12:12Z`, `observed_on 2026-10-30`; 10 KB-8 MB per file, 0.4-0.9 s.
- **Upstream:** scraped from the lgdirectory.gov.in reports. Source headers: `S.No., State Code, State Name (In English), District Code, District Name(In English), Census 2001 Code, Census 2011 Code`. Today: 790 districts, 7,099 subdistricts, 7,343 blocks, 677,671 villages.

| State | Districts | Subdistricts | Blocks | Villages |
|---|---|---|---|---|
| PB | 23 | 97 | 161 | 13,014 |
| HR | 23 | 143 | 143 | 7,090 |
| DL | 13 | 39 | - | - |
| CH | 1 | 1 | 1 | - |

**D. DataMeet maps. [OK] Too old for districts.**
- **URL:** `https://raw.githubusercontent.com/datameet/maps/master/Districts/Census_2011/2011_Dist.shp` (+`.dbf/.shx/.prj`). Reading the attributes remotely through `/vsicurl/` took 3.8 s.
- **Counts:** 641 districts; PB 20, HR 21, DL 9, CH 1. Columns `DISTRICT, ST_NM, ST_CEN_CD, DT_CEN_CD, censuscode`.
- **Licence:** CC BY 2.5 IN; the repo default is CC BY 4.0. Last push 2022-05-11.
- **Also in the repo:** `Country/india-soi.geojson` (12.3 MB, CC BY-SA 2.5/ODbL, dissolved from Census-2011/SOI) and `india-composite.geojson` (CC0).

**E. geoBoundaries. [OK] ADM1 is fine, ADM2 is stale.**
- **API:** `https://www.geoboundaries.org/api/current/gbOpen/IND/ADM{0,1,2,3}/` (1.0-1.3 s). It returns `gjDownloadURL` and `simplifiedGeometryGeoJSON` on GitHub LFS (ADM2 simplified: 8.0 MB in 2.4 s).
- **ADM1:** 36 units, `shapeISO` like `IN-PB`. Has Ladakh, Telangana and DNH&DD, and its extents match SOI (Ladakh to 37.077N), so the depiction is official. The metadata claims "2011, CC BY 2.5 IN".
- **ADM2:** 735 features, metadata "2021, Pathways/LGD, ODbL". It has **no Charkhi Dadri and no Malerkotla**, uses Gurgaon/Mewat/Muktsar/Firozpur, and has no state or LGD field.
- **ADM3:** 6,836 units (2018).
- **ADM0:** Wikimedia, 2014.

**F. Bhuvan (ISRO/NRSC) WMS. [WARN] Live and official, but raster-only in the browser.**
- **Endpoint:** `https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms`. GetCapabilities: 11 MB, 7,245 layers, 11.4 s; `Fees NONE`, `AccessConstraints NONE`.
- **Useful layers:** `basemap:STATE_BDY_UPD` (post-2019 J&K/Ladakh), `basemap:INDIA_STATE`, `sdv:INDIA_DIST` (pre-2021: a point in Malerkotla returns `{'DIST': 'SANGRUR'}`), `sdv:PB_tehsil` (`{'NEW_TEH': 'SANGRUR', 'TALUK': 'Sangrur'}`), `sdv:PB_BlockBound`, `sdv:PB_District_HQ`.
- **Speed:** GetMap 0.4-0.9 s. GetFeatureInfo with `info_format=application/json` returns GeoJSON *with geometry* in 0.3-0.7 s.
- **Limits:** WFS is off (`"Service WFS is disabled"`); no `Access-Control-Allow-Origin` header, so the browser can't read it; the Bhuvan logo is watermarked into GetMap images; `bhuvan-vec2` timed out after 30 s.
- **Use:** backend-only, as an optional "ISRO cross-check" agent tool.

**G. Direct government services. [FAIL] today.**
- `onlinemaps.surveyofindia.gov.in` and `indiamaps.gov.in`: TCP connect timeouts (21-30 s).
- Bharatmaps `https://mapservice.gov.in/gismapservice/rest/services/BharatMapService/Admin_Boundary_Village/MapServer?f=json` returns `{"error":{"code":499,"message":"Token Required"}}`.
- The LGD web services in NIC doc v1.6 (2019) are dead: `https://lgdirectory.gov.in/webservice/lgdws/districtList` (POST `stateCode`) returns 404. lgdirectory.gov.in itself answers 200, but its downloads are behind a captcha.

## 3. Vintage: what changed since the datasets were cut (codes from today's LGD snapshot)

| State | Districts today | Changes | Polygon availability |
|---|---|---|---|
| Punjab | 23 | Malerkotla (737), carved from Sangrur: announced 14-May-2021, cabinet approval 2-Jun-2021 (news / india.gov.in) | LGD-2023 has it. SOI, geoBoundaries and DataMeet do not |
| Haryana | 23 | **Hansi (792)**: gazette 682-ARIC-03-2025/7392 of 19-Dec-2025, per news + RBI circular. Made of tehsils Hansi 396 and Narnaund 395, and sub-tehsils Bass 6408 and Kheri Jalab St 6419. Earlier: Charkhi Dadri 701 (2016), Mewat renamed Nuh (604) | No dataset has it. **Derive it** (sec. 5) |
| Delhi | 13 | GNCTD notification of 25-Dec-2025 (news): adds Outer North 794, Old Delhi 795, Central North 796; drops Shahdara 671 | No open polygons. Use the SOI NCT outline. Label any 11-district layer "pre-Dec-2025" |
| Chandigarh | 1 | - | - |

## 4. Names: join on LGD codes, never on names

| LGD today (code) | SOI `District_C` | LGD-2023 `dtname` | geoBoundaries ADM2 | DataMeet 2011 |
|---|---|---|---|---|
| Ferozepur (31) | FIROZPUR | Ferozepur | Firozpur | Firozpur |
| Sri Muktsar Sahib (39) | SRI MUKTSAR SAHIB | Sri Muktsar Sahib | Muktsar | Muktsar |
| S.A.S Nagar (608) | SAS NAGAR (SAHIBZADA AJIT SINGH NAGAR) | S.A.S Nagar | Sahibzada Ajit Singh Nagar | Sahibzada Ajit Singh Nagar |
| Gurugram (62) | GURUGRAM | Gurugram | Gurgaon | Gurgaon |
| Nuh (604) | MEWAT | Nuh | Mewat | Mewat |
| Charkhi Dadri (701) | CHARKHI DADRI | Charki Dadri | missing | missing |
| Malerkotla (737) / Hansi (792) | missing / missing | present / missing | missing | missing |

- On Wikidata, the city item Q238508 is labelled "Gurgaon", while GeoNames says "Gurugram".
- **FIRMS** points carry only lat/lon, so assign them with `ST_Intersects` on the LGD polygons.
- **CPCB** station/city spellings are **UNVERIFIED** today: `cpcb.nic.in` timed out, `airquality.cpcb.gov.in/AQI_India/` is an SPA, and data.gov.in needs a key. Map each station to a district by its lat/lon, not by its city string.
- Keep an alias table (`lgd_code, alias`) built from the columns above. Add CPCB aliases once that feed is wired.

## 5. Ingest recipe (tested on the local `postgis/postgis:17-3.5`, port 5433, using TEMP tables)

Reading GeoParquet needs `pyarrow`, which is **not** in the env (`uv add pyarrow`).

For large layers, use the `iomaps` CLI (PyPI; ~81 dependencies incl. duckdb). It does bbox-pushdown range reads:

```bash
# 8-13 s per layer. On Git Bash, set MSYS_NO_PATHCONV=1 or pass the source *name*:
# a route like "/subdistricts/soi/" gets rewritten to "C:/Program Files/Git/subdistricts/soi/".
uv run --with iomaps iomaps cli extract --source "LGD Subdistricts" --bounds '73.8,27.6,77.7,32.6' -o lgd_subdistricts_nw.gpkg
#   other sources: "SOI Subdistricts", "LGD Blocks", "LGD Villages", "Census 2011 Village Points"
# Districts/states are small enough to download whole:
curl -L -o LGD_Districts.parquet https://github.com/ramSeraph/indian_admin_boundaries/releases/download/districts/LGD_Districts.parquet
```

Load the polygons. This is the tested core, with TEMP tables swapped for real ones:

```python
import geopandas as gpd, psycopg
ST = {3: "Punjab", 6: "Haryana", 7: "Delhi", 4: "Chandigarh"}
d = gpd.read_parquet("LGD_Districts.parquet", columns=["dtname", "dist_lgd", "state_lgd", "geometry"])
d = d[d.state_lgd.isin(ST)]
s = gpd.read_file("lgd_subdistricts_nw.gpkg"); s = s[s.state_lgd.isin(ST)]
with psycopg.connect("postgresql://blindspot:blindspot@localhost:5433/blindspot") as con, con.cursor() as cur:
    cur.execute("""CREATE TABLE stg_sub(lgd_code int, name text, district_lgd int, state_lgd int, geom geometry);
      CREATE TABLE adm_subdistrict(lgd_code int PRIMARY KEY, name text, district_lgd int, state_lgd int, geom geometry(MultiPolygon,4326));
      CREATE TABLE adm_district(lgd_code int PRIMARY KEY, name text, state_lgd int, src text, geom geometry(MultiPolygon,4326));""")
    cur.executemany("INSERT INTO stg_sub VALUES (%s,%s,%s,%s, ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(%s,4326)),3))",
        [(int(r.subdt_lgd), r.sdtname, int(r.dist_lgd), int(r.state_lgd), r.geometry.wkb) for r in s.itertuples()])
    cur.execute("""INSERT INTO adm_subdistrict SELECT lgd_code, min(name), min(district_lgd), min(state_lgd), ST_Multi(ST_Union(geom))
                   FROM stg_sub GROUP BY lgd_code""")          # dissolves duplicate codes (Loharu 401)
    cur.executemany("INSERT INTO adm_district VALUES (%s,%s,%s,'lgd-bharatmaps-2023', ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(%s,4326)),3)))",
        [(int(r.dist_lgd), r.dtname, int(r.state_lgd), r.geometry.wkb) for r in d.itertuples()])
```

Derive Hansi (792). Tested: all **79/79** village polygons of today's Hansi fall inside it, and **0/150** of Hisar's and
**0/105** of Bhiwani's do. Area: 1,376 km². Using the tehsil union alone (1,319 km²) misses 5 villages that moved in from
Hisar and Bawani Khera tehsils.

```sql
-- vil(vil_lgd, subdt_2023, dist_now, geom) = LGD_Villages polygons, with dist_now taken from TODAY's LGD villages parquet
WITH hansi AS (SELECT ST_Multi(ST_Union(geom)) g FROM (
       SELECT geom FROM adm_subdistrict WHERE lgd_code IN (395,396)
       UNION ALL SELECT geom FROM vil WHERE dist_now = 792 AND subdt_2023 NOT IN (395,396)) x)
INSERT INTO adm_district SELECT 792,'Hansi',6,'derived: tehsils 395+396 + reassigned villages', g FROM hansi;
UPDATE adm_district h SET geom = ST_Multi(ST_CollectionExtract(ST_Difference(h.geom, n.geom),3)), src = h.src||' minus Hansi'
FROM adm_district n WHERE h.lgd_code IN (63,59) AND n.lgd_code = 792;      -- Hisar, Bhiwani
CREATE INDEX ON adm_district USING gist(geom);
```

Test results:

| Check | Result |
|---|---|
| Polygons per state | PB 23, CH 1, HR 23, DL 11 |
| In today's LGD, no polygon | 794, 795, 796 (new Delhi districts) |
| Polygon, gone from today's LGD | 671 (Shahdara) |
| (75.84, 30.25) | Sangrur 43 / tehsil Sangrur 271 |
| Malerkotla point | Malerkotla 737 |
| Narnaund point | Hansi 792 / Narnaund 395 |
| Load + lookups | 0.4 s |

Serve as MVT from FastAPI. Tested: z7/90/52 gives 23 features, 68.8 kB, 58 ms. For comparison, GeoJSON of all 57
districts is 238 kB at `ST_SimplifyPreserveTopology(...,0.002)` and 580 kB at 0.0005.

```sql
WITH b AS (SELECT ST_TileEnvelope(:z,:x,:y) env),
q AS (SELECT lgd_code, name, ST_AsMVTGeom(ST_Transform(geom,3857), b.env, 4096, 64, true) geom
      FROM adm_district, b WHERE ST_Intersects(ST_Transform(geom,3857), b.env))
SELECT ST_AsMVT(q, 'districts', 4096, 'geom') FROM q;   -- store a geom_3857 column + index in prod
```

Provenance:
- Keep a `dataset_provenance(name, url, sha256, retrieved_at, vintage, licence)` row per file. The LGD index already ships sha256 values.
- Show provenance in an "About data" panel. This answers the judges' "no static data" question: everything comes from official open datasets via a re-runnable ingest job.

## 6. Villages (Punjab)

**[OK] LGD village polygons (Bharatmaps 2023, "not-so-open").**
- iomaps extract of `"LGD Villages"`, bbox `73.85,29.5,76.95,32.52`: 11 s, 38 MB GPKG, **12,832 Punjab polygons**.
- Sample row: `{vilname11:'Kakar (3)', vil_lgd:37194, vilcode11:'037194', dtname:'Amritsar', sdtname:'Ajnala', gp_name:'KAKAR', block_name:'CHOGAWAN', block_lgd:212}`.
- Today's LGD lists 13,014 Punjab villages.

**[OK] Census-2011 village points (Bharatmaps).**
- **12,708 Punjab points**, 11,954 of them with `t_pop2011>0` (sum 17.69 M). Extract takes 9 s.
- Sample row: `{vilname:'Bugar (82)', vilcode11:'039786', DT_LGD:43, SDT_LGD:271, t_pop2011:1232, Lat:30.21497, Long:75.59171, GP_NAME:'BUGRA'}`.
- This is the right source for "people near a burn".

**[OK] SOI village points (`"SOI Village Points"`).**
- 9,218 Punjab points, all of them with `lgd_district_code`.

**[OK] OSM Overpass**, endpoint `https://overpass-api.de/api/interpreter`:

```
[out:json][timeout:180];
area["ISO3166-2"="IN-PB"]["admin_level"="4"]->.pb;
nwr["place"~"^(village|hamlet)$"](area.pb);
out center tags qt;           // use `out count;` for the count only
```

- **Count:** `{"nodes":"7633","ways":"248","relations":"1","total":"7882"}`, query time 44.8 s. **Full export:** 1.26 MB in 42.4 s, `timestamp_osm_base 2026-10-30T16:27:21Z` (village nodes 7,172, hamlet nodes 461, village ways 243, hamlet ways 5, relations 1).
- **Tag quality:** 7,542 have `name`, 176 `population`, 292 `wikidata`, and **1** carries a census code.
- **Sample:** `{"type":"node","id":6694798091,"lat":29.5824613,"lon":75.235733,"tags":{"name":"Man Khera","place":"village"}}`.
- **[WARN] Reliability:** between attempts, overpass-api.de returned `502 "at capacity"` and `504 Dispatcher_Client timeout`; kumi.systems and maps.mail.ru gave `504`; private.coffee hung for 90 s. Run it as a batch job only, cache the result, never call it on page load.

**[OK] DataMeet `indian_village_boundaries` (ODbL) has no Punjab or Haryana.**
- The repo contains only br, ga, gj, ka, kl, mh, or, rj and sk. Last push 2018.
- SHRUG Census-2011 village polygons do exist (`/shrug-census2011/villages/`) but are CC BY-NC-SA. Not tested.

## 7. Receptor cities: Wikidata, with GeoNames fallback (snippet tested end-to-end as written)

```python
import csv, io, json, urllib.parse, urllib.request, zipfile, httpx
NAMES = ["Delhi","Gurugram","Noida","Faridabad","Ghaziabad","Chandigarh","Ludhiana","Amritsar","Jalandhar",
         "Patiala","Bathinda","Hisar","Panipat","Karnal","Sonipat","Rohtak","Jaipur","Lucknow"]   # config, not data
BBOX = (73.5, 26.0, 81.5, 32.6)      # W,S,E,N - drops namesakes (e.g. a 'Chandigarh' village in West Bengal, Q60623068)
UA = {"User-Agent": "BlindSpot/0.1 (+https://github.com/<team>/blindspot)"}   # put real contact info here
SPARQL = """SELECT ?name ?item ?itemLabel ?pop ?popDate ?lat ?lon WHERE {
  VALUES ?name { %s }
  ?item rdfs:label|skos:altLabel ?name ; wdt:P17 wd:Q668 ;
        p:P625/psv:P625 [ wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lon ] .
  FILTER EXISTS { ?item wdt:P31/wdt:P279* wd:Q486972 }   # human settlement
  FILTER NOT EXISTS { ?item wdt:P31 wd:Q1149652 }        # not 'district of India'
  OPTIONAL { ?item p:P1082 ?ps . ?ps ps:P1082 ?pop ; a wikibase:BestRank . OPTIONAL { ?ps pq:P585 ?popDate } }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }""" % " ".join(f'"{n}"@en' for n in NAMES)

def wikidata():      # stdlib urllib on purpose: WDQS answered 403 to httpx 0.28.1 but 200 to urllib/requests/curl
    req = urllib.request.Request("https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": SPARQL}),
                                 headers={**UA, "Accept": "application/sparql-results+json"})
    best = {}
    for b in json.load(urllib.request.urlopen(req, timeout=60))["results"]["bindings"]:
        lat, lon = float(b["lat"]["value"]), float(b["lon"]["value"])
        if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]): continue
        c = dict(name=b["name"]["value"], qid=b["item"]["value"].rsplit("/", 1)[1], label=b["itemLabel"]["value"], lat=lat, lon=lon,
                 pop=int(float(b["pop"]["value"])) if "pop" in b else None, pop_date=b.get("popDate", {}).get("value", "")[:10] or None, pop_src="wikidata")
        k = lambda c: (c["pop"] is not None, c["pop_date"] or "", c["pop"] or 0)      # prefer: has pop, newest, largest
        if c["name"] not in best or k(c) > k(best[c["name"]]): best[c["name"]] = c
    return best

def geonames_fill(rec):   # CC BY 4.0 daily dump, 3.4 MB, keyless
    z = httpx.get("https://download.geonames.org/export/dump/cities15000.zip", timeout=120)
    rows = [r for r in csv.reader(io.StringIO(zipfile.ZipFile(io.BytesIO(z.content)).read("cities15000.txt").decode()), delimiter="\t")
            if r[8] == "IN" and BBOX[0] <= float(r[5]) <= BBOX[2] and BBOX[1] <= float(r[4]) <= BBOX[3]]
    for n in [n for n in NAMES if n not in rec or rec[n]["pop"] is None]:
        m = [r for r in rows if n.lower() in {r[1].lower(), r[2].lower(), *[a.strip().lower() for a in r[3].split(",")]}]
        if m:
            r = max(m, key=lambda r: int(r[14] or 0))
            rec.setdefault(n, dict(name=n, qid=None, label=r[1], lat=float(r[4]), lon=float(r[5])))
            rec[n].update(pop=int(r[14]), pop_date=None, pop_src=f"geonames:{r[0]}")
    return rec
```

Today's output (sample only; the app must fetch it; this exact snippet re-run in 4.5 s + 2.7 s):
- Delhi Q1353 (28.6667, 77.2167) 26,495,000 (2016); Gurugram Q238508 (label "Gurgaon") 876,824; Noida Q278204 637,272; Faridabad Q200663 1,404,653; Ghaziabad Q207098 2,375,820.
- Chandigarh Q43433 (30.7333, 76.7797) 1,055,450; Ludhiana Q200123 1,618,879; Amritsar Q48403 1,159,227 (2020); Jalandhar Q33377 873,725; Patiala Q208162 406,192 (no date); Bathinda Q709319 285,813.
- Hisar Q1772825 307,024; Rohtak Q1453180 374,292; Jaipur Q66485 3,073,350; Lucknow Q47916 3,382,000 (2017).
- Population from GeoNames: Panipat Q139706 295,970 (GeoNames 1260476); Karnal Q256194 302,140 (1267708); Sonipat Q41561 289,333 (1255744).
- Most Wikidata values are Census 2011. Store `pop_src` and `pop_date` with each row.
- **[WARN] WDQS rate limit:** it twice returned `HTTP 429 "Aggressively rate-limiting to 1 req / min - this rule was created during active wdqs outage"`. Fetch once a day in the ingest job, retry after 65 s, and fall back to GeoNames coordinates if it is down.
- A rule-based alternative (wikibase:box + population ≥ 250k) works but takes 42 s and returns noise (e.g. "Mahasi", 546,995).

## 8. Basemaps

**8.1 CARTO dark-matter. [WARN] Works keyless today, but a key is now required by the terms.**
- **Style:** `https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json`: HTTP 200, 67 KB, 1.1 s, CORS `*`. It is now `"Dark Matter (Protomaps)"`: 95 layers, one source `protomaps`, tiles `https://tiles.basemaps.cartocdn.com/v/20261026-01/vectortiles/carto.streets/v2/{z}/{x}/{y}.mvt` (0.2-0.5 s, keyless 200), TileJSON `.../vector/carto.streets/v2/tiles.json` (Protomaps 4.15.2).
- **Terms:** the README (updated 2026-10-21) says "An API key is required to use CARTO basemaps... free for non-commercial use up to 5M tile requests a month and for commercial use up to 1M". Get a key at `https://carto.com/basemaps/apikey/` and append `?key=` to the style URL. The vector schema moved to Protomaps on 2026-10-21, and self-hosted copies of the style break when the `/v/<release>/` URLs change.
- **Raster without a key is watermarked:** `https://a.basemaps.cartocdn.com/dark_all/7/90/51.png` returned a 200 tile reading "API KEY REQUIRED" (checked visually). `carto.com/legal/basemap-terms/`: no server-side caching or proxying, browser cache at most 30 days, credit OpenStreetMap and CARTO.
- **Boundary layer ids** (source-layer `boundaries`): `boundary_county`, `boundary_state`, `boundary_country_outline`, `boundary_country_inner`.
- **Region labels:** `place_state` (`kind=region`), `place_country_1`, `place_country_2`.

**8.2 OpenFreeMap. [OK] Recommended.**
- **Styles:** `https://tiles.openfreemap.org/styles/{liberty|bright|positron|dark|fiord}`. All returned 200, 21-49 KB, 0.3-0.8 s, CORS `*`.
- **Source:** `https://tiles.openfreemap.org/planet` (TileJSON), which points to `planet/20261025_080012_pt/{z}/{x}/{y}.pbf`. OpenMapTiles 3.16, maxzoom 14, tiles 0.4-0.6 s.
- **Boundary fields:** `admin_level, adm0_l, adm0_r, claimed_by, disputed, disputed_name, maritime`.
- **Boundary layer ids:**

| Styles | Boundary layer ids |
|---|---|
| liberty / bright / positron | `boundary_3`, `boundary_2`, `boundary_disputed` |
| dark / fiord | `boundary_state`, `boundary_country_z0-4`, `boundary_country_z5-` |

- **Region labels:** `label_state` (liberty, bright, positron) or `place_state` (dark, fiord). These draw "Azad Kashmir" and "Gilgit-Baltistan", so filter them.
- **Terms** (openfreemap.org): "no registration, no user database, no API keys"; "no limits on the number of map views or requests"; commercial use yes; attribution required (MapLibre adds it automatically from the TileJSON); no SLA.

**8.3 EOX Sentinel-2 cloudless. [OK]**
- **Capabilities:** `https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml` (68 KB, 3.8 s) lists `s2cloudless` (2016) through `s2cloudless-2025`, each also as `_3857`.
- **XYZ template:** `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2025_3857/default/g/{z}/{y}/{x}.jpg`. Note **{y} before {x}**.
- **Checked over Sangrur:** z10/421/727 (19 KB, 1.0 s) and z14/6746/11643 (18 KB). Both are real imagery, checked visually (fields, Sangrur town).
- **Headers:** CORS `*`, `max-age=604800`. z15 and z16 also return tiles, but they are upsampled, so set `maxzoom: 14` on the source.
- **Licence:** 2017-2025 layers: "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2025) released under CC BY-NC-SA 4.0. For commercial usage please see https://cloudless.eox.at". The 2016 `s2cloudless_3857` layer is **CC BY 4.0**.

**8.4 Esri World Imagery. [WARN] Works keyless, but the terms need an account.**
- **Tiles:** `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}`: 200, 25 KB, 0.44 s, CORS `*`, 24 LODs.
- **Credit:** `copyrightText` is "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community". The item licence is the Esri Master License Agreement ("not intended to be used to export tiles for offline").
- **Esri attribution docs:** require the text **"Powered by Esri"** plus the service `copyrightText`, and "An ArcGIS Location Platform or ArcGIS Online account is required" for the Basemap Styles service.
- **UNVERIFIED** (no key): whether keyless use of the legacy endpoint outside ArcGIS is allowed, and the keyed endpoints themselves. A key would unlock Esri's Basemap Styles and static tiles, via a free ArcGIS Location Platform account.

**8.5 indianopenmaps SOI tiles (fallback for SOI lines). [OK]**
- **Tiles:** `https://indianopenmaps.com/{states|districts}/soi/{z}/{x}/{y}.pbf` (verified; `/subdistricts/soi/` is in the `/api/routes` catalogue but I did not fetch a tile). CORS `*`, 2.0 s cold.
- **TileJSON:** `.../states/soi/tiles.json`, layer `SOI_States`, maxzoom 11. Districts: layer `SOI_Districts`, maxzoom 10.
- **Credit** (from TileJSON): "© Survey Of India - Collected by Datameet Community".
- **Hosting:** a hobby Cloudflare Worker, so use it as a fallback only.
- **Gotcha:** GitHub release `.pmtiles` support Range requests (206), but the 302 from github.com carries **no CORS header**, so the browser can't load `pmtiles://` from GitHub directly.

## 9. MapLibre: strip foreign boundaries, add SOI + imagery

Tested with Node 24 and `@maplibre/maplibre-gl-style-spec@26.5.0` `validateStyleMin`, against the live OFM dark,
positron, liberty and CARTO dark-matter styles:
- 0 validation errors.
- Every listed id exists.
- No `boundary`/`boundaries` layer is left.

```js
const BOUNDARY_LAYERS = {
  'openfreemap:liberty': ['boundary_3', 'boundary_2', 'boundary_disputed'],
  'openfreemap:positron': ['boundary_3', 'boundary_2', 'boundary_disputed'],
  'openfreemap:bright': ['boundary_3', 'boundary_2', 'boundary_disputed'],
  'openfreemap:dark': ['boundary_state', 'boundary_country_z0-4', 'boundary_country_z5-'],
  'openfreemap:fiord': ['boundary_state', 'boundary_country_z0-4', 'boundary_country_z5-'],
  'carto:dark-matter': ['boundary_county', 'boundary_state', 'boundary_country_outline', 'boundary_country_inner'],
};
const BAD_REGION_LABELS = ['Azad Kashmir', 'Gilgit-Baltistan', 'Aksai Chin'];
export function indiaCompliant(style, key, soiTiles /* our /tiles/admin/{z}/{x}/{y}.pbf or indianopenmaps */) {
  const drop = new Set(BOUNDARY_LAYERS[key]);
  const labelLayers = new Set(['label_state', 'place_state']);
  style.layers = style.layers.filter((l) => !drop.has(l.id)).map((l) => labelLayers.has(l.id) && l.filter
    ? { ...l, filter: ['all', l.filter, ['!', ['in', ['coalesce', ['get', 'name:en'], ['get', 'name']], ['literal', BAD_REGION_LABELS]]]] }
    : l);
  style.sources.eox_s2 = { type: 'raster', tileSize: 256, maxzoom: 14,
    tiles: ['https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2025_3857/default/g/{z}/{y}/{x}.jpg'],
    attribution: '<a href="https://cloudless.eox.at">Sentinel-2 cloudless</a> by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2025), CC BY-NC-SA 4.0' };
  style.sources.soi_states = { type: 'vector', tiles: [soiTiles], minzoom: 0, maxzoom: 11,
    attribution: 'Boundaries: Survey of India (via DataMeet / indianopenmaps)' };
  const firstSymbol = style.layers.findIndex((l) => l.type === 'symbol');
  style.layers.splice(firstSymbol < 0 ? style.layers.length : firstSymbol, 0,
    { id: 'eox-s2', type: 'raster', source: 'eox_s2', layout: { visibility: 'none' } },          // imagery toggle
    { id: 'soi-state-line', type: 'line', source: 'soi_states', 'source-layer': 'SOI_States',   // our MVT: use its layer name
      paint: { 'line-color': '#9aa4b1', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.6, 8, 1.6] } });
  return style;
}
// usage: const s = indiaCompliant(await (await fetch('https://tiles.openfreemap.org/styles/dark')).json(), 'openfreemap:dark',
//        'https://indianopenmaps.com/states/soi/{z}/{x}/{y}.pbf');  new maplibregl.Map({ style: s, ... })
```

- Draw deck.gl layers (burn scars, FIRMS, particles) through `MapboxOverlay({interleaved: true})` with `beforeId: 'soi-state-line'`, so the boundaries and labels stay on top.
- If a raster basemap with baked-in borders is ever used, `@india-boundary-corrector/maplibre-protocol` (ramSeraph) repaints those borders in the tiles. Not tested.

## 10. Attribution strings (put all of them in the MapLibre `customAttribution` / "About data")

| Data | Credit |
|---|---|
| OpenFreeMap | OpenFreeMap © OpenMapTiles Data from OpenStreetMap (auto-added from TileJSON) |
| CARTO, only if used with a key | © OpenStreetMap contributors © CARTO |
| EOX | Sentinel-2 cloudless - https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2025), CC BY-NC-SA 4.0 |
| Esri, only if used | Powered by Esri - Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community |
| Boundaries | International & state boundaries: Survey of India digital boundary data (per DST Geospatial Guidelines 2021). District/tehsil/village polygons: NIC Bharatmaps / Local Government Directory (MoPR), collated by DataMeet & ramSeraph (CC0) |
| Places | © OpenStreetMap contributors (ODbL); Wikidata (CC0); GeoNames (CC BY 4.0) |
| Bhuvan, only if used | Bhuvan, NRSC/ISRO |

## 11. Gotchas (all hit today)

1. **Overpass:** at capacity for most of an hour. Batch it, cache it, and keep a list of mirrors.
2. **WDQS:** 429 at 1 req/min during its outage, and 403 to httpx (use urllib/requests). Space calls 65 s or more apart.
3. **Git Bash path conversion:** it rewrites `/route/` CLI arguments. Use `MSYS_NO_PATHCONV=1`. Also pass Windows-style paths (`C:/...`) to Python, which can't read `/c/...`.
4. **Data cleaning before PostGIS:**
   - SOI names carry diacritics; use the `*_C` columns.
   - LGD polygons can repeat a code (dissolve them).
   - Every layer has a few invalid rings: `ST_MakeValid` + `ST_CollectionExtract(...,3)`.
5. **Bhuvan WMS** has no CORS and a watermark; call it from the backend only.
6. **Bharatmaps REST** needs a token; LGD's 2019 web services are 404; the SOI portals time out. Plan on the ramSeraph mirrors and record that in the provenance table.
7. **Stale "2021" metadata** on geoBoundaries ADM2: the content is ~2016. It has Telangana's 2016 "Yadadri Bhongiri" but lacks Haryana's Charkhi Dadri (2016; LGD `year_stat=2016_c`) and Malerkotla (2021). Don't trust vintage fields; count districts instead.
8. **CARTO** switched to the Protomaps schema on 2026-10-21. Older blog snippets that filter `admin_level`/`disputed` on CARTO no longer apply; use the `kind`/`kind_detail` ids above.

## 12. Not verified / blocked

- CPCB city/station spellings (`cpcb.nic.in` timed out; data.gov.in needs `DATA_GOV_IN_API_KEY`).
- The SOI licence text on the SOI portal itself (portal unreachable).
- The DST PDF (404; quoted via ramSeraph).
- The exact Hansi boundary against the gazette map. The derivation is validated on village membership only, and 18 of its 97 villages have no polygon.
- Polygons for Delhi's 13 districts (none open).
- Esri keyed endpoints and the CARTO key flow (no keys).
- OSM admin_level 5 district relations (not queried; Overpass was overloaded).
