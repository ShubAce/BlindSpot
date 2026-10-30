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

`ENABLE_SCHEDULER=1` makes the API process run the scheduler instead. Run the scheduler in one
process only (one uvicorn worker, or the CLI). Each job is also locked in Postgres, so a CLI run
and the scheduler never run the same job at the same moment. Every run is stored in `job_run`; on
restart an interval job waits until it is next due instead of running again straight away.

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
