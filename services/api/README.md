# BlindSpot API (`services/api`)

The backend: ingest jobs that pull real data from upstream APIs into PostGIS, and a FastAPI
service that serves it with provenance on every number. The contract (tables, endpoints, jobs) is
[`docs/architecture.md`](../../docs/architecture.md).

Commands below run from `services/api` unless they say otherwise. Python 3.12 and the
dependencies are managed by [uv](https://docs.astral.sh/uv/).

## 1. Keys and settings

Copy `.env.example` (repo root) to `.env` (repo root) and fill in the free keys. `.env` is
gitignored; never commit it and never paste a key into code, chat or a log. The backend reads `.env`
from the repo root whatever directory you start it from.

| Variable | Default | Used for |
| --- | --- | --- |
| `GEMINI_API_KEY`, `FIRMS_MAP_KEY`, `DATA_GOV_IN_API_KEY`, `OPENAQ_API_KEY` | unset | upstream APIs (all optional) |
| `DATABASE_URL` | `postgresql+psycopg://blindspot:blindspot@localhost:5433/blindspot` | PostGIS |
| `ENABLE_SCHEDULER` | `0` | run the job scheduler inside the API process |
| `CORS_ORIGINS` | `http://localhost:3000` | comma-separated or JSON list |
| `CORS_ORIGIN_REGEX` | unset | origins allowed by pattern too (full match), e.g. the website's preview URLs |
| `INIT_DB_ON_STARTUP` | `true` | create missing tables at start-up (`false` on serverless) |
| `CACHE_DIR` | `<repo>/data/cache` | downloads and rendered imagery |
| `AOI_BBOX` / `MET_BBOX` | `73.8,27.6,77.7,32.6` / `70,26,80,34` | area of interest / wind-field box |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | absolute URLs the browser can reach |
| `LOG_LEVEL` | `INFO` | |

Keys are `SecretStr` in `blindspot.config.Settings`: call `.get_secret_value()` only where the key is
sent upstream. Logs pass through a formatter that masks every configured key, and `httpx`'s own
request logging is switched off because some upstream URLs carry a key.

## 2. Start the database

From the repo root:

```sh
docker compose -f infra/docker-compose.yml up -d     # PostGIS 3.5 on localhost:5433
```

Tables are created by `init_db()` when the API or a job starts (idempotent). `create_all` never
alters an existing table: if you change your module's columns during development, drop your own
table in the dev database (`docker exec -it blindspot-db psql -U blindspot -c 'DROP TABLE my_table'`)
and restart.

## 3. Run the API

```sh
uv run uvicorn blindspot.api.main:app --reload --port 8000
curl http://localhost:8000/api/health      # {"ok":true,"db":true,"version":"0.1.0",...}
curl http://localhost:8000/api/sources     # freshness of every upstream source
```

Interactive docs: <http://localhost:8000/docs>.

If the database is not up yet when the API starts, the API serves anyway (`/api/health` says
`"db": false`) and keeps retrying `init_db()` in the background until the tables are in place.

## 4. Run jobs

```sh
uv run python -m blindspot.jobs list            # jobs, schedules, last runs
uv run python -m blindspot.jobs run firms_nrt   # one job now (then the jobs that run after it)
uv run python -m blindspot.jobs run-all         # every job once: one-off loads, periodic, daily
uv run python -m blindspot.jobs scheduler       # the scheduler in the foreground (Ctrl+C stops)
```

**First load, in dependency order** (each job is idempotent; `--no-chain` stops a job from also
starting the jobs that run after it, so you control the order and the Gemini budget):

```sh
for j in boundaries firms_nrt firms_backfill creams_daily tle air air_dss met \
         sentinel risk featured smoke smoke_validate geostationary geostationary_backfill bulletin; do
  uv run python -m blindspot.jobs run "$j" --no-chain
done
```

| Order | Job | Needs | Typical time |
| --- | --- | --- | --- |
| 1 | `boundaries` | nothing; labels every other table's state/district | ~3 min |
| 2 | `firms_nrt`, `firms_backfill`, `creams_daily` | boundaries (state polygons for clipping) | seconds |
| 3 | `tle` | nothing (skips when CelesTrak was asked < 2 h ago) | seconds |
| 4 | `air`, `air_dss` | boundaries (city labels) | ~3 min, 2 s |
| 5 | `met` | nothing (skips when the model runs are unchanged) | ~75 s |
| 6 | `sentinel` | fires (for "unseen"), boundaries; `SENTINEL_BUDGET_MIN` caps one run (default 50) | 15-50 min |
| 7 | `risk`, `featured` | sentinel | ~30 s, ~2 min |
| 8 | `smoke`, `smoke_validate` | met, fires, sentinel scars, air | ~20 s, 6 s |
| 9 | `geostationary`, `geostationary_backfill` | fires (validation), boundaries | 7 s, ~5 min |
| 10 | `bulletin` (then `featured_investigations`) | everything above; uses Gemini | ~1-2 min |
| daily | `prune` | nothing; deletes old smoke/met/risk runs, job runs, orphan fetches (`PRUNE_DRY_RUN=1` only counts) | seconds |

`ENABLE_SCHEDULER=1` makes the API process run the scheduler instead. Run the scheduler in one
process only (one uvicorn worker, or the CLI). Each job is also locked in Postgres, so a CLI run
and the scheduler never run the same job at the same moment. Every run is stored in `job_run`; on
restart an interval job waits until it is next due instead of running again straight away.

## 4a. Production: what runs where

| Piece | Where | Notes |
| --- | --- | --- |
| Web API (`blindspot.api.main:app`) | Vercel Hobby, one Python function, region `sin1` (Singapore) | reads Neon; no scheduler, no table creation; `maxDuration` 60 s, body cap 4.5 MB |
| CDN | Vercel, in front of the function | keeps 200 GETs until the job group feeding them next runs (expiry clocks in `api/cache.py`; contract notes "Serverless deployment") |
| Stored answers | `response_body` in Neon | finished bodies of heavy routes (smoke trails, scar polygons, boundaries), xz-compressed; the smoke jobs write theirs ahead of any request. Budget and cadence: `docs/neon-budget.md` |
| Database | Neon free tier (sized for 0.5 GB of storage; the plan's compute, egress and storage limits are in `docs/neon-budget.md`), Postgres 18 + PostGIS, `aws ap-southeast-1` | the API and the jobs use the **direct** host (NEON_DB without `-pooler`) |
| Jobs | GitHub Actions (`.github/workflows/data-jobs.yml`), writing to Neon | fires, CREAMS, air and GK-2A hourly 08:17-18:17 UTC and at 00:17, 03:17, 06:17, 21:17 (air runs beside the other three); met (-> smoke) 03:17, 09:17, 15:17, 21:17; Sentinel-2 (-> risk, featured), smoke check and element sets 11:17; bulletin 02:00 with retries 03:17, 06:17; backfills and prune 06:17; boundaries Monday 03:17 (every group shares a wake-up with an hourly run: `docs/neon-budget.md`); secrets set by `infra/set_github_secrets.py` |
| Per-client limits | `client_hit` in Neon (salted address hashes, a few rows a minute) | agent questions, live picture renders and live map tiles are counted across every instance; until the table exists the count fails open (each instance's in-memory limit only). Contract notes, "Agent" and "Imagery" |
| Pictures | `rendered_asset` in Neon, written by the `featured` job | frames, the 24 largest scars' chips and the featured manifest; any instance reads them |

The function runs in Singapore beside Neon: from Vercel's default region (`iad1`) every database
round trip costs ~220 ms. Secrets live only in the repo-root `.env`; the scripts below read them
from there and pass them on stdin or in the process environment, never on a command line.

## 4b. Load Neon

`infra/load_neon.py` copies the local database to Neon, compacted: simplified boundaries, burn-risk
factors stored once per run (`risk_run`) plus each field's own numbers, and only the smoke and
weather runs the `prune` job keeps. It creates the schema first (`init_db`), refuses a local or
`-pooler` target and is idempotent: it **empties the app's tables on the target** and copies them
again (~1 min), so rows the jobs wrote on Neon since (fresh fires, newer risk runs) are replaced
by the local copy. Re-run it only when the local data should win, e.g. after a schema change.

Set `DATABASE_URL` to Neon's direct host in the process environment (it wins over `.env`), never
in a file. PowerShell:

```powershell
$line = Get-Content F:\forge\.env | Where-Object { $_ -match '^NEON_DB=' } | Select-Object -First 1
$env:DATABASE_URL = (($line -replace '^NEON_DB=', '').Trim().Trim("'").Trim('"')) -replace '-pooler\.', '.'
cd F:\forge\services\api
uv run python ..\..\infra\load_neon.py                    # only when the local data should win
uv run python -m blindspot.jobs run featured --no-chain    # frames + chips + manifest into rendered_asset
Remove-Item Env:DATABASE_URL                               # before anything else, above all pytest
```

- A new table only needs `init_db`, which every `python -m blindspot.jobs run ...` performs first;
  `create_all` adds missing tables and never alters existing ones.
- Never run `pytest` with `DATABASE_URL` pointing at Neon: conftest would create a test database
  there.
- Size: 348 MB after the load and three risk runs; 333 MB on 2026-10-31 (sized for a 0.5 GB cap; see `docs/neon-budget.md`). The daily `prune` job and the
  `risk` job's changed-rows-only update keep it there; `docs/build-logs/deploy-data.md` has the
  per-table sizes and the egress of each endpoint.

## 4c. Deploy the web API to Vercel

```powershell
cd F:\forge\services\api
uv run python ..\..\infra\deploy_vercel_api.py          # full bundle, env vars, production deploy
```

The script assembles a work folder (every `blindspot/*.py`, a root `index.py` exposing `app`, a
`requirements.txt` pinned from `uv.lock` for `WEB_PACKAGES` only, `vercel.json` with the region
and `maxDuration`), links project `blindspot-api`, rewrites the production environment variables
and runs `vercel deploy --prod`. It needs `VERCEL_TOKEN` and `NEON_DB` in `.env`. Options:
`--skip-env` (keep the variables), `--cors URL,...` (extra exact origins), `--public-url URL`
(default `https://blindspot-api-azure.vercel.app`), `--foundation-only`.

Environment it sets on Vercel:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Neon direct host |
| `GEMINI_API_KEY`, `FIRMS_MAP_KEY`, `DATA_GOV_IN_API_KEY`, `OPENAQ_API_KEY`, `LSASAF_*` | from `.env` (encrypted) |
| `ENABLE_SCHEDULER` | `0` (the jobs run on GitHub Actions) |
| `INIT_DB_ON_STARTUP` | `false` (cold starts skip `create_all`) |
| `CACHE_DIR` | `/tmp/blindspot-cache` (per instance, starts empty) |
| `PUBLIC_BASE_URL` | the production address: the picture URLs in `/api/imagery/featured`, case files and tilejson |
| `CORS_ORIGINS` / `CORS_ORIGIN_REGEX` | `["http://localhost:3000"]` / `^https://blindspot[a-z0-9-]*\.vercel\.app$` |
| optional, not set by the script | `CLIENT_HASH_SALT` (else derived from `DATABASE_URL`), `IMAGERY_RENDER_PER_MINUTE`/`_PER_DAY` (30/300), `IMAGERY_TILES_PER_MINUTE`/`_PER_DAY` (600/6000), `SHARED_CLIENT_LIMITS=0` (per-instance counting only) |

Bundle: the web packages (fastapi ... planetary-computer) plus pillow, rasterio and affine for
drawing pictures, about 282 MB of the 500 MB cap. Job-only libraries (geopandas, pandas, scipy,
h5py, pyarrow, lightgbm, pdfplumber, pyproj...) are not shipped.

- **Import rule:** importing the app and every router must not import a heavy module. Heavy
  imports live inside the job-time functions that need them. `tests/test_integration.py` proves it
  by importing the app in a subprocess with those modules made unimportable; pillow and rasterio
  are imported only when a picture is drawn.
- **Pictures:** an instance looks in its `/tmp`, then `rendered_asset`, then renders live (1-20 s).
  Run `featured` against Neon after a load so the landing frames and the largest scars' chips are
  stored; the daily Sentinel-2 run keeps them current.
- **Payloads:** two default bodies exceed 4.5 MB raw and rely on gzip (browsers always send it):
  `/api/smoke/forward` and `/api/scars` without filters. See the contract notes, "Limits".
- Each warm instance also keeps hot responses in memory for 30-300 s (`blindspot/api/cache.py`).
- **Neon budget** (100 CU-hours, 5 GB egress a month; `docs/neon-budget.md`): a cold request
  reads at most ~300 KB for every website query except `/api/risk?limit=1000` (the live map's
  risk layer, ~0.9 MB: the risk route keeps no stored answer yet) and the first build of a `/live`
  district list after its scars change (~1.4 MB). Heavy answers come from `response_body`
  (`cache.stored_response`), which the first job run after a deploy creates (`init_db`). Stamps hash
  the source with line endings normalised (`cache.source_text`), so bodies the Linux job runner
  stores are served by an API deployed from Windows. A never-seen heavy variant (`guard=True`) is
  limited to 20 per 10 minutes per instance (429 after). Never add a poller that reaches the
  database every few minutes: it keeps the compute awake all month.

After a deploy, check from outside (the scratch checks used in `docs/build-logs/deploy-api.md`
do all of this): every contract GET returns 200 with the numbers of the local API (Neon is a
snapshot, so small drifts are expected), error paths keep their 4xx, every body is under 4.5 MB on
the wire, `/api/imagery/featured` frame URLs start with the production address and load, `POST
/api/agent/ask` with an example question streams `run ... done`, `Access-Control-Allow-Origin`
echoes `https://blindspot-web.vercel.app` and `http://localhost:3000`, and a repeated GET shows
`x-vercel-cache: HIT` (or `STALE`).

## 5. Run the tests

```sh
uv run pytest
```

Tests need the Docker database and an internet connection (one tiny keyless Open-Meteo call).
They run against a separate `blindspot_test` database, which `tests/conftest.py` creates on first
use, so they never touch the data the app serves. Tests may use recorded real responses only, under
`tests/fixtures/`.

## 6. Layout and ownership

```
blindspot/
  config.py      settings (.env at the repo root), secret masking, logging setup      foundation
  db.py          engine, SessionLocal, session_scope(), get_session, Base, init_db()   foundation
  http.py        fetch(): shared client, retries, cooldown, source_fetch logging       foundation
  provenance.py  SOURCE_REGISTRY, record_fetch/track_fetch, SourceRef, source_status   foundation
  geo.py         AOI constants, IST, bbox helpers, district / nearest-place lookups    foundation
  models/core.py source_fetch, boundary, place, job_run                                foundation
  api/main.py    app factory, CORS, gzip, JSON errors, /api/health, /api/sources      foundation
  api/__init__.py, api/responses.py   SessionDep, BBoxDep, respond(), ORJSONResponse  foundation
  models/<m>.py  sources/<m>.py  analytics/<m>.py  jobs/<m>.py  api/routers/<m>.py      module
tests/conftest.py (foundation), tests/test_<m>.py (module)
```

**Ownership rule.** A module owns its files in `models/`, `sources/`, `analytics/`, `jobs/`,
`api/routers/` and its own tests. The shared files marked "foundation" above are owned by the
foundation: other modules only import them. Need something changed there? Ask; do not edit it.
New files are picked up automatically: `models/`, `jobs/` and `api/routers/` import every module
in their folder, and a module that fails to import is logged and skipped instead of breaking the rest.

## 7. Writing a module

**Fetch upstream data** with `blindspot.http.fetch`. It retries timeouts, connection errors, 5xx
and 429 (up to 3 attempts, Retry-After honoured up to 60 s), pauses a source for 5 minutes after
3 failed calls in a row (`SourceUnavailable`), and writes one `source_fetch` row per call with
secrets masked in the URL.

```python
from blindspot.http import FetchError, fetch, set_fetch_records

result = fetch("openmeteo", "https://api.open-meteo.com/v1/forecast",
               params={"latitude": 30.9, "longitude": 75.85, "hourly": "wind_speed_925hPa"})
rows = parse(result.data)
set_fetch_records(result.fetch_id, len(rows))     # how many records this fetch produced
```

`expect="text"` or `"bytes"` for CSV and images. Keys in query parameters named `key`, `api_key`,
`api-key`, `MAP_KEY` and the like are masked, and so is any configured key anywhere in the URL.
For calls that do not go through `fetch` (pystac-client, google-genai, rasterio reads), wrap them:

```python
from blindspot.provenance import track_fetch

with track_fetch("stac_pc", url=search_url) as tracker:
    items = list(search.items())
    tracker.records = len(items)
```

Use a key of `provenance.SOURCE_REGISTRY` (`firms`, `celestrak`, `openaq`, `datagov_aqi`, `cams`,
`openmeteo`, `stac_pc`, `stac_e84`, `pc_data`, `worldcover`, `boundaries`, `osm`, `gemini`). To add
a source or refine its label or docs link, call `register_source(SourceInfo(...))` from your own file.

**Tables** go in `models/<m>.py`, subclassing `blindspot.db.Base`, SRID 4326, with a `fetch_id`:

```python
fetch_id: Mapped[int | None] = mapped_column(
    BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True)
```

**Jobs** go in `jobs/<m>.py`. They must be idempotent; `run_job` catches and records failures.

```python
from blindspot.jobs import job

@job("firms_nrt", every_minutes=30)
def firms_nrt() -> dict:
    """Keyless FIRMS 24 h CSVs -> fire_detection."""
    ...
    return {"rows": n}                    # kept in job_run.result

@job("smoke", after="met")                # runs each time `met` succeeds
@job("bulletin", cron="30 7 * * *")       # crontab, Asia/Kolkata time
```

**Routes** go in `api/routers/<m>.py`, as plain `def` functions, reading the database only:

```python
from fastapi import APIRouter
from blindspot.api import BBoxDep, SessionDep, respond
from blindspot.provenance import latest_refs

router = APIRouter(prefix="/api/fires", tags=["fires"])

@router.get("")
def fires(session: SessionDep, bbox: BBoxDep, hours: int = 24):
    ...
    return respond({"hours": hours, "count": n, "features": fc},
                   latest_refs(session, ["firms"]), max_age=30)
```

`respond()` adds `generated_at` and `sources`, writes datetimes as UTC with `Z`, handles numpy
arrays, and is much faster than returning a big dict. Errors are JSON
(`{detail, status, path, generated_at, sources}`); an uncaught `FetchError` becomes a 502 and
`SourceUnavailable` a 503 with `Retry-After`.

**Places**: `blindspot.geo` has `district_for_point`, `admin_for_points` (many points, one query),
`nearest_place`, `fill_admin(session, "my_table")` (label stored rows once boundaries exist),
`parse_bbox`, `bbox_envelope`, `point_ewkt`, `AOI_BBOX`, `MET_BBOX` and `IST`. They return `None`
until the boundaries module has loaded `boundary` and `place`; leave a name blank, never guess one.

## 8. House rules

- **No dummy data.** Every number comes from a live API, an official open dataset, or a computation
  over them. When a source is missing or down, say so and show when data was last good.
- **Provenance.** Every upstream call leaves a `source_fetch` row; every response lists its `sources`.
- **Times** are stored and served in UTC (ISO-8601 with `Z`); IST is for display and schedules.
- **Secrets** never appear in code, URLs that get logged, error messages or commits.

## For module authors

Short rules for adding a module (fires, orbits, air, met, smoke, sentinel, risk, agent). §7 has
longer examples; `docs/architecture.md` §4–6 fixes the table, endpoint and job names.

**Files.** Add yours in `models/<m>.py`, `sources/<m>.py`, `analytics/<m>.py`, `jobs/<m>.py`,
`api/routers/<m>.py` and `tests/test_<m>.py`. Nothing else needs editing: each folder imports every
file in it. A file that fails to import is logged ("could not import ...") and skipped, so check
the log when your table, route or job is missing. Never edit the foundation files; ask instead.

**Models.** Subclass `blindspot.db.Base`, use SRID 4326, and give every row the fetch it came from:

```python
from datetime import datetime

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base


class FireDetection(Base):  # a few of its columns; the full list is in architecture.md section 4
    __tablename__ = "fire_detection"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str]
    acq_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # always tz-aware UTC
    lat: Mapped[float]
    lon: Mapped[float]
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))  # GiST index is automatic
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    __table_args__ = (UniqueConstraint("source", "acq_utc", "lat", "lon"),)  # idempotent upserts
```

- A model file may import `blindspot.db`, `blindspot.models.core`, `blindspot.provenance`,
  `blindspot.http`, `blindspot.geo` and `blindspot.config`; never `blindspot.api` or `blindspot.jobs`.
- `init_db()` creates missing tables but never alters one: after changing columns, drop your own
  table in the dev database and restart.
- The dev database (the PostGIS Docker image) also has `tiger` and `topology` schemas on its search
  path, with tables named `place`, `state`, `county`, `layer`... `init_db()` puts ours in `public`,
  which comes first. In psql, use `\dt public.*` to see only ours.
- Insert points with `geom=point_ewkt(lon, lat)` (from `blindspot.geo`). Upsert with
  `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(...)` or `on_conflict_do_update`.

**Upstream calls.** Use `fetch()`. It returns `FetchResult(data, fetch_id, status_code, latency_ms,
url_logged, source_key, fetched_at, headers)` and has already written the `source_fetch` row:

```python
from blindspot.config import get_settings
from blindspot.http import fetch, set_fetch_records

# inside a job (or a sources/ function the job calls)
key = get_settings().firms_map_key                 # SecretStr, or None when not configured
if key is None:
    return {"skipped": "FIRMS_MAP_KEY is not set"}  # say so; never substitute made-up values
result = fetch("firms", f"{FIRMS}/api/area/csv/{key.get_secret_value()}/{product}/{area}/1/{day}",
               expect="text", meta={"product": product})
rows = parse_csv(result.data)
set_fetch_records(result.fetch_id, len(rows))      # how many records this call produced
# ...then store result.fetch_id on every row you write
```

- Keys go in params, headers or (FIRMS) the path, at the call site only. `fetch` masks key-like
  parameter names and any configured key wherever it appears, in the URL, error, meta and logs.
  Still, never print a key or copy one into an exception message.
- `FetchError` means the call failed after its retries and is already recorded. `SourceUnavailable`
  means the source is paused after repeated failures and nothing was sent. In a job, let them
  propagate (the run is recorded as failed) or catch them per item to keep partial results. In a
  route they become 502 or 503 on their own.
- For an SDK call that cannot go through `fetch` (pystac-client, google-genai, rasterio), wrap it in
  `with track_fetch("stac_pc", url=...) as t:` and set `t.records`. Use a key from
  `SOURCE_REGISTRY`, or add yours with `register_source(SourceInfo(...))` in your own file.

**Jobs.** A plain function in `jobs/<m>.py`, named as in architecture.md §6:
`@job("firms_nrt", every_minutes=30)`, `@job("bulletin", cron="30 7 * * *")` (crontab in IST;
APScheduler counts day-of-week from 0 = Monday, so write `mon-fri`, not numbers), or
`@job("smoke", after="met")`. It must be idempotent and should return a small JSON-able summary
(kept in `job_run.result`). Raise to fail: `run_job` records the error and never lets it reach the
scheduler or the API. Try it with `uv run python -m blindspot.jobs run <name>`. Importing
`blindspot.jobs` registers every job, so `run_job("<name>")` also works in tests.

**Routes.** `api/routers/<area>.py` defines `router = APIRouter(prefix="/api/<area>", tags=[...])`.

- Routes are plain `def` (the database and `fetch` calls block) and read the database only.
- Take `session: SessionDep` and `bbox: BBoxDep` from `blindspot.api`.
- Always `return respond(payload, sources, max_age=30)`. A plain `dict` gets no `generated_at`
  or `sources`, and its times come out as `+00:00` instead of `Z`.
- Build GeoJSON in SQL (`ST_AsGeoJSON(geom)::json`); coordinates are `[lon, lat]`.
- `raise HTTPException(404, "no such scar")` gives the standard JSON error body.

**SourceRef.** Every response lists where its numbers came from.

- Data served from tables: `latest_refs(session, ["firms"])` gives the last successful fetch per
  source; a source never fetched comes back with nulls, which is honest.
- Exact rows: `ref_for_fetch(session, row.fetch_id)`.
- An on-demand call: `result.ref()`.

**Places and time.** `admin_for_points`, `district_for_point`, `nearest_place` and
`fill_admin(session, "<table>")` return `None` (or change 0 rows) until the boundaries module has
loaded `boundary` and `place`; leave the name blank, never guess it. Store and serve UTC
(`provenance.utcnow()`); use `geo.IST` only for display and day boundaries.

**Tests.** `uv run pytest` runs against `blindspot_test`, never the dev database. Use real recorded
responses only, under `tests/fixtures/`. Check that your routes return `generated_at` and `sources`.
