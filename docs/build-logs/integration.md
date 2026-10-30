# Integration build log (2026-10-31, 04:19-05:15 IST)

## Done

**1. Every job ran for real, in dependency order** (`python -m blindspot.jobs run <job> --no-chain`,
`SENTINEL_BUDGET_MIN=15`). All 16 succeeded:

| Job | Result |
| --- | --- |
| boundaries | ok, 163 s (LGD unchanged; OSM 4 states; 22 cities) |
| firms_nrt | ok, 4.6 s |
| firms_backfill | ok, 8.2 s (44 days already complete per source) |
| creams_daily | ok, 1.4 s (30 Oct layer, 44 events; 31 Oct not published yet) |
| tle | ok, skipped by the CelesTrak 2 h rule |
| air | ok, 205 s (OpenAQ 196 calls, 352 hourly rows; CAMS run already stored) |
| air_dss | ok, 1.7 s (no new DSS version) |
| met | ok, skipped: both model runs fetched 1.2 h earlier and unchanged |
| sentinel | ok, 972 s (punjab 43SER/43RFQ/43RFP, haryana 43RFM, ...; burn_cluster 51,365 -> 67,854) |
| risk | ok, 780 s (172,130 fields; it was 30 s for 30k) |
| featured | ok, 93.5 s (4 frames, 144 chips) |
| smoke | ok, 15.4 s (forward 572 fires, 22 back runs, 2 members) |
| smoke_validate | ok, 6.2 s (Delhi r 0.65; best lag 4 h, r 0.85) |
| geostationary | ok, 13.8 s (9 new scans) |
| geostationary_backfill | ok, 19.3 s |
| bulletin | ok, 121 s (en/hi verified; pa flagged `script_ok=false`) |

Dev DB afterwards:

| Table | Rows |
| --- | --- |
| fire_detection | 11,730 |
| official_fire_event | 2,696 |
| burn_cluster | 67,854 |
| harvest_cluster | 172,130 |
| s2_scene processed | 142 |
| air_obs | 41,153 |
| smoke_run | 185 |
| met_grid | 8,640 |
| geo_fire_slot | 439 |
| geo_fire_pixel | 4,276 |
| bulletin | 6 |
| agent_run | 13 |
| place | 20,790 |
| boundary | 412 |
| tle | 8 |

**2. GET /api/stats/headline**: `blindspot/analytics/headline.py` + `blindspot/api/routers/stats.py`.

- Six blocks:
  - fires_24h: India-polygon clipped, static sites excluded; the same rule as /api/fires.
  - season: FIRMS 4,782 vs CREAMS 1,043 official.
  - candidate unseen burns: 86,303 of 104,476 ha (0.826), precision 0.87 [0.68, 0.95], n=23.
  - Delhi PM2.5: measured only. It is null and stale overnight, so the last value is shown with
    its age, plus the smoke-trace verdict.
  - Last/next VIIRS-MODIS look at the Punjab centroid.
  - GK-2A evening activity: 476 pixel-hours in 16:30-20:41 IST; 76% outside the polar windows.
- Each block is isolated: a failure becomes `{available:false, reason}`.
- Cached for 60 s. Cold 265 ms, warm 1.5 ms.

**`blindspot/api/cache.py`**:

- `get_or_build` is a single-flight TTL cache.
- `cached_route` caches a route's 200 response bytes.
- Applied to:
  - air: latest, series, stubble-share
  - scars: list, summary
  - geostationary: diurnal, latest
  - smoke: receptors, validation
  - risk
  - met: now, wind-field
  - bulletin/latest
  - geo/boundaries
  - fires (its private cache now delegates to the shared one)

**3. Serverless readiness.** Importing the app and all 13 routers (42 paths) pulls in no heavy
module. This is proven by a subprocess that sets `sys.modules[...] = None` for rasterio,
geopandas, pyogrio, pandas, scipy, lightgbm, h5py, pyarrow, xarray, odc, dask, pdfplumber,
sklearn, PIL and pyproj (`tests/test_integration.py::test_api_imports_without_heavy_modules`).
Edits that move heavy imports into job-time functions:

- `analytics/fires_stats.py`: scipy into `find_static_sites` (firms job only).
- `analytics/trajectory.py`: `cKDTree` into `_expose` (smoke job only).
- `analytics/geofire.py`, `analytics/burnscar.py`: `scipy.ndimage` behind `_ndimage()`;
  rasterio `features` into the cluster step. `FOUR_CONNECTED` is now a literal numpy cross,
  identical to `generate_binary_structure(2, 1)`. `Affine` is imported under TYPE_CHECKING.
- `sources/boundaries.py`: geopandas and pyarrow imported inside the readers.
- `sources/pcdata.py`: PIL imported inside `check_image` / `encode`.
- `sources/gk2a.py`: h5py, pyproj and pandas imported inside the functions that use them.
- `config.py`: `INIT_DB_ON_STARTUP` (default true). `api/main.py`'s lifespan skips `init_db`
  when it is false.
- `api/main.py`: an `ImportError` in a route becomes a 503 `{missing_module}` instead of a 500
  (imagery rendering on serverless).

**4. Every endpoint exercised on uvicorn :8000.** All 41 GET endpoints returned 200 with real
data; the p50 table is in `docs/api-contract-notes.md`. POST /api/agent/ask ran once live
(8.4 s, gemini-3.5-flash-lite, 10/10 numbers verified, 232 detections, the same figure as the
headline); 9 replays from the answer cache had a p50 of 146 ms. Error paths checked:

| Request | Status |
| --- | --- |
| unknown smoke city | 404 with the city list |
| region=IN-PB | 422 |
| unknown scar id | 404 |
| bad bbox | 400 |
| lat=95 | 422 |
| bad imagery collection | 422 |
| unknown agent run id | 404 |
| lang=fr | 422 |
| unknown air city | 404 |
| unknown GK-2A slot | 404 |

`api/main.py`: GZip `compresslevel=6` (Starlette's default is 9). On the 1.4 MB boundaries body,
gzip took 251 ms at level 9 and 52 ms at level 6, for 0.3% more bytes. The boundaries p50 went
from 261 ms to 58 ms.

**5.** `docs/api-contract-notes.md` is written.

**6.** `uv run pytest -q`: 216 passed. The 6 new tests are in `tests/test_integration.py`.
`tests/conftest.py` gained an autouse fixture that clears the response cache before each test,
because two module tests read an endpoint straight after writing rows. `/api/agent/examples` is
not cached for the same reason. README updated: run order, the serverless section, and
`INIT_DB_ON_STARTUP`. uvicorn is stopped.

## Next / open

- The pa bulletin for 2026-10-31 is still `script_ok=false`: Gemini mixes Bengali and Cyrillic
  letters into Gurmukhi. It is flagged and not counted as written; the hourly retries
  (07:30-12:30 IST) need the scheduler running. The UI must hide or warn on it.
- Risk now takes 780 s for 172k fields. The all-pairs backtest AUC fell to 0.556 over 148 pairs
  as new tiles arrived (the headline pair is 0.687). Present the weaker number too.
- Delhi `pm25` is null for hours at night (the OpenAQ delay). The headline shows `last` and its
  age.
- Serverless can serve imagery only from the disk cache. Rendering needs rasterio and pillow,
  which the Vercel package list does not include: a 503 with `missing_module`.
- The sentinel coverage is still partial (remaining punjab and haryana tiles). The 6-hourly job
  continues.
