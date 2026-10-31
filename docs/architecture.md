# BlindSpot — architecture and contracts

BlindSpot finds the Punjab/Haryana crop-residue fires that polar-orbiting fire satellites miss. It maps burn scars
from Sentinel-2, flags scars that no NASA FIRMS fire alert explains ("unseen burns"), ranks harvested-but-unburnt
fields as the next burning risk, traces smoke to cities with a wind-driven particle model, and runs a Gemini
function-calling agent that investigates air-quality spikes and writes cited bulletins.

## 1. Non-negotiable principles

1. **No dummy data.** Every number shown anywhere comes from a live API, an official open dataset, or a computation
   over those. No hardcoded figures, no mock fixtures in the app, no seeded fake rows. Tests may use recorded real
   responses only under `tests/fixtures/` and never ship to the app.
2. **Provenance on every number.** Every fetch is logged in `source_fetch`; every API response carries `sources: SourceRef[]`;
   the agent cites them. The UI shows freshness ("fetched 4 min ago").
3. **Serve from the database, ingest with jobs.** API routes read PostGIS (fast, always available). Upstream APIs are
   only called by scheduled jobs (and a few on-demand analyses with timeouts). If an upstream is down, the app keeps
   serving the last good real data and says how old it is.
4. **Degrade honestly.** A failed block shows what failed and when data was last good. Never invent a fallback value.
5. **Explainable code.** Small modules, docstrings that say what and why, no magic. Every team member must be able to
   explain it to judges.

## 2. Components

```
                 ┌──────────── jobs (APScheduler in-process, or CLI / GitHub Actions) ────────────┐
 NASA FIRMS ─────┤ firms_nrt 30m · firms_backfill daily                                           │
 CelesTrak ──────┤ tle 12h                                                                        │
 OpenAQ/CPCB ────┤ air 30m (OpenAQ v3 → CPCB stations; data.gov.in if key; CAMS model)             ├──► PostGIS
 Open-Meteo ─────┤ met 3h (multi-level wind grid, boundary layer) → smoke 3h (particle model)      │
 Sentinel-2 STAC ┤ sentinel 6h (new clear scenes → burn scars, crop state) → risk                  │
 Gemini ─────────┤ bulletin daily 07:30 IST                                                       │
                 └────────────────────────────────────────────────────────────────────────────────┘
                                   │
                         FastAPI  /api/*  (reads DB; SSE agent; imagery proxy+cache)
                                   │
                 Next.js  /  /live  /case/[id]  /bulletin  /method   (MapLibre + deck.gl)
```

## 3. Repository layout

```
services/api/
  blindspot/
    config.py            settings from env (.env at repo root)
    db.py                engine, SessionLocal, Base, init_db() (postgis extension + create_all)
    http.py              shared httpx client: timeouts, retries, provenance logging
    provenance.py        SourceRef, record_fetch(), source registry (key, label, cadence, docs_url)
    geo.py               AOI constants, district/village/city lookups (uses boundary/place tables)
    models/              one file per module; models/__init__.py auto-imports every file
      core.py            source_fetch, boundary, place
      fires.py  orbits.py  air.py  met.py  smoke.py  sentinel.py  agent.py
    sources/             upstream connectors (pure fetch + parse, no DB writes)
      firms.py  celestrak.py  openaq.py  datagov.py  openmeteo.py  stac.py  boundaries.py
    analytics/           computations (pure functions where possible)
      fires_stats.py  passes.py  trajectory.py  burnscar.py  harvest.py  risk.py  headline.py
    agent/               tools.py  runner.py  verifier.py  prompts.py  bulletin.py
    api/
      main.py            app factory; auto-includes every router module in api/routers/
      routers/           one file per area, each exposes `router = APIRouter(prefix=...)`
    jobs/
      __init__.py        @job(name, every_minutes, after=None) registry; auto-imports jobs/*.py
      __main__.py        CLI: list | run <name> | run-all | serve-scheduler
      firms.py  air.py  met.py  smoke.py  orbits.py  sentinel.py  bulletin.py
  tests/                 pytest; real-response fixtures only
apps/web/                Next.js 16 app (see §9)
docs/                    architecture, research, method
infra/                   docker-compose (PostGIS), deploy configs
```

Ownership rule for parallel work: a module owns its files in `models/`, `sources/`, `analytics/`, `jobs/`,
`api/routers/` and its tests. Shared files (`config.py`, `db.py`, `http.py`, `provenance.py`, `geo.py`,
`api/main.py`) are owned by the foundation; others only import them.

## 4. Data model (PostGIS, SRID 4326)

| Table | Key columns |
| --- | --- |
| `source_fetch` | id, source_key, url (no secrets), started_at, finished_at, ok, http_status, records, latency_ms, error, meta jsonb |
| `boundary` | id, level (state/district/subdistrict), name, state, code, geom MultiPolygon, source |
| `place` | id, kind (village/town/city), name, district, state, lat, lon, geom Point, population, source |
| `fire_detection` | id, source (VIIRS_NOAA20_NRT…), satellite, instrument, acq_utc, lat, lon, geom, confidence, frp_mw, bright_ti4, bright_ti5, scan, track, daynight, state, district, fetch_id · unique(source, acq_utc, lat, lon) |
| `tle` | norad_id pk, key, name, line1, line2, epoch, fetched_at, fetch_id |
| `air_obs` | id, provider, station_id, station_name, city, state, lat, lon, geom, parameter (pm25/pm10), value, unit, observed_at, is_model, fetch_id · unique(provider, station_id, parameter, observed_at) |
| `met_grid` | id, run_at, valid_at, level ("10m"/"950hPa"/"925hPa"/"850hPa"), lon0, lat0, dlon, dlat, nx, ny, u real[], v real[], blh real[] (10m row only), fetch_id |
| `smoke_run` | id, kind (forward/back), city (back only), run_at, t0, hours, params jsonb, summary jsonb, trails jsonb, receptors jsonb |
| `s2_scene` | item_id pk, collection, datetime, mgrs_tile, cloud_pct, bbox, status, processed_at, error |
| `burn_cluster` | id, geom Polygon, centroid, area_ha, burn_after, burn_before (dates of the clear passes bracketing the burn), scene_pre, scene_post, dnbr, nbr_post, confidence, unseen, fire_matches, district, subdistrict, village, model_version, created_at |
| `harvest_cluster` | id, geom, centroid, area_ha, harvested_after, harvested_before, last_seen_unburnt, scene_last, district, village, risk_score, risk_factors jsonb |
| `agent_run` | id, kind (ask/bulletin), question, lang, model, started_at, finished_at, events jsonb, answer, citations jsonb, verification jsonb, ok |
| `bulletin` | id, date, lang, generated_at, model, content jsonb, run_id |

## 5. HTTP API (all JSON unless noted; times ISO-8601 UTC with `Z`; IST is a display concern)

Every JSON response includes `generated_at` and `sources: SourceRef[]` where
`SourceRef = {key, label, url|null, fetched_at|null, fetch_id|null}`.
GeoJSON uses `[lon, lat]`. Query `bbox=minLon,minLat,maxLon,maxLat`. Default AOI: Punjab+Haryana+Delhi-NCR.

| Method · path | Params | Returns |
| --- | --- | --- |
| GET `/api/health` | | `{ok, time, db, version}` |
| GET `/api/sources` | | `{sources:[{key,label,kind,status:"ok"\|"stale"\|"error"\|"never",last_success_at,last_attempt_at,last_error,records_last,latency_ms_last,cadence_minutes,docs_url}]}` |
| GET `/api/fires` | hours=24, bbox, min_confidence | `{hours,count,by_satellite:{},features:FeatureCollection<Point,{id,satellite,instrument,acq_utc,confidence,frp_mw,daynight,state,district}>}` |
| GET `/api/fires/daily` | since, region=all\|punjab\|haryana | `{series:[{date,count,frp_sum}]}` |
| GET `/api/fires/hours` | days=7, region | `{total,bins:[{hour_ist,count}],by_satellite:{sat:[24 counts]},observed_windows_ist:[{start,end}],unobserved_span_ist:{start,end}}` |
| GET `/api/orbits/tle` | | `{satellites:[{key,name,norad_id,instrument,swath_km,line1,line2,epoch,fetched_at}]}` |
| GET `/api/orbits/passes` | lat, lon, hours_back=24, hours_ahead=48 | `{site,passes:[{satellite,instrument,start_utc,peak_utc,end_utc,peak_elevation_deg,min_ground_distance_km}],current:{watching:[{satellite,until_utc}],next:{satellite,at_utc}}}` |
| GET `/api/scars` | since, until, district, unseen, bbox, limit | `{totals:{clusters,area_ha,unseen_clusters,unseen_area_ha},features:FeatureCollection<Polygon,{id,area_ha,burn_after,burn_before,district,subdistrict,village,centroid,dnbr,confidence,unseen,fire_matches,scene_pre,scene_post}>}` |
| GET `/api/scars/summary` | since | `{season:{since,clusters,area_ha,unseen_area_ha,unseen_share,fire_detections},by_district:[{district,state,area_ha,clusters,unseen_area_ha,unseen_share,fire_detections}]}` |
| GET `/api/scars/{id}` | | `{case:{…properties,geometry},imagery:{pre:{date,item_id,collection,truecolor_url,swir_url,nbr_url},post:{…}},fires_nearby:[…],weather_at_burn:{…}}` |
| GET `/api/risk` | district, limit | `{as_of,totals:{clusters,area_ha},features:FeatureCollection<Polygon,{id,area_ha,harvested_after,harvested_before,days_since_harvest,risk_score,risk_factors,district,village,centroid}>,ranking:[{village,district,area_ha,clusters,risk_score}]}` |
| GET `/api/brief` | days=7 | district field brief index: `{window:{from,to,days,newest_pass},districts:[{district,state,unseen_clusters,unseen_area_ha,villages,harvested_fields,top_tenth_fields,top_tenth_villages,links}],totals,candidate_unseen_audit,risk_run,ranking_reliability,notes}`; the window ends at the newest Sentinel-2 pass, not today |
| GET `/api/brief/{district}` | days=7, limit=50 | `{district,state,window,totals,villages:[{village,subdistrict,unseen_clusters,unseen_area_ha,newest_pass,lat,lon,case_ids,maps_url,case_url}],watchlist:[{village,subdistrict,fields,top_tenth_fields,in_top_tenth,best_rank,lat,lon,field_ids,maps_url}],watchlist_in_top_tenth,candidate_unseen_audit,risk_run,ranking_reliability,notes}` |
| GET `/api/brief/{district}/csv` | days=7, lang=en\|hi\|pa | text/csv (UTF-8 with BOM): one row per village; the count and area header cells carry the window and the audit precision with its 95% CI |
| GET `/api/air/latest` | | `{stations:[{id,name,city,state,lat,lon,pm25,pm10,observed_at,provider,is_model}],receptors:[{city,pm25_now,pm25_24h_mean,observed_at,provider,n_stations}]}` |
| GET `/api/air/series` | city, hours=72 | `{city,series:[{t,pm25,n_stations}],model_series:[{t,pm25}]}` |
| GET `/api/met/now` | city | `{city,t,wind_10m:{speed_kmh,from_deg},wind_925:{speed_kmh,from_deg},blh_m,temp_2m_c,inversion,rain_mm_24h}` |
| GET `/api/met/wind-field` | t=now, level=925hPa | `{t,level,grid:{lon0,lat0,dlon,dlat,nx,ny},u:[],v:[]}` |
| GET `/api/smoke/forward` | | `{run_at,t0,hours,step_minutes,inputs:{fires,scars},trails:[{id,path:[[lon,lat]],times:[unix_s],weight,origin:{type,id,district}}],receptors:[{city,series:[{t,smoke_index}],peak:{t,value},arrival_t}]}` |
| GET `/api/smoke/back` | city, hours=72 | `{run_at,city,t0,trails:[…],summary:{share_over_burn_belt:{h24,h48,h72},fire_exposure_index,top_districts:[{district,share}],blh_m_now,wind_from_deg_now},verdict_hint:"stubble_likely"\|"mixed"\|"stubble_unlikely"}` |
| GET `/api/imagery/featured` | | `{aoi:{name,bbox},frames:[{date,item_id,collection,cloud_pct,truecolor_url,swir_url,width,height}]}` |
| GET `/api/imagery/render` | collection, item, bbox, kind=truecolor\|swir\|nbr, w, h | image/jpeg or image/png, cached on disk, `Cache-Control: public, max-age=31536000, immutable` |
| GET `/api/stats/headline` | | `{fires_24h:{count,by_satellite,window_ist},season:{…},delhi:{pm25,observed_at,provider},passes:{last,next}}` |
| POST `/api/agent/ask` | `{question, lang?, follow_up_of?}` | `text/event-stream` of events below; 400 when `follow_up_of` names a run that cannot be followed up (unknown, not an investigation, no answer, over 24 h old) |
| GET `/api/agent/runs/{id}` · `/api/agent/examples` | | a stored run, with `follow_up_of`, `context`, `follow_up_until` · recent good runs (never follow-ups) |
| GET `/api/agent/stats` | days=30 | guardrail counts from every stored run: `{window,basis,questions:{runs,answered,failed,figures,all_matched,corrected_figures,honesty,duration_ms,tool_calls,models,languages,series},bulletins:{…},definitions}`; a field older runs lack is `unknown`, never false |
| GET `/api/bulletin/latest` | lang=en\|hi\|pa | `{date,lang,generated_at,model,title,summary,sections:[{heading,body}],citations}` |

Agent SSE events (one JSON object per `data:` line):
`run{run_id,model,lang,follow_up_of?,context?}` · `thought{text}` · `tool_call{id,name,args}` · `tool_result{id,name,ok,summary,sources}` ·
`map{action:"fly_to"|"show_layer"|"highlight",payload}` · `answer_delta{text}` · `answer{text,citations:[{marker,source}]}` ·
`verification{checked,verified,unverified:[str],passed}` · `error{message,retryable}` · `done{run_id,duration_ms}`.

## 6. Jobs

Each job is idempotent, records a `source_fetch` per upstream call, catches its own errors and never takes the API
down. `uv run python -m blindspot.jobs run <name>` runs one; the API process runs the scheduler when
`ENABLE_SCHEDULER=1`.

The cadences below are the in-process scheduler's (local development). In production the jobs run on GitHub
Actions on the cron slots in `.github/workflows/data-jobs.yml`: fires, official counts, air and GK-2A hourly
from 13:47 to 23:47 IST and about every 3 h otherwise; weather (then smoke) every 6 h; Sentinel-2 (then risk)
and orbits daily at 16:47 IST; the bulletin daily at 07:30 IST.

| Job | Every | Does |
| --- | --- | --- |
| `firms_nrt` | 30 min | keyless 24h CSVs (VIIRS J1, J2, SNPP; MODIS) → upsert `fire_detection` with state/district |
| `firms_backfill` | daily | FIRMS area API (MAP_KEY) for the season since Sep 15 in ≤10-day windows |
| `tle` | 12 h | CelesTrak GP for the fire and imaging satellites → `tle` |
| `air` | 30 min | OpenAQ v3 latest for stations in the AOI (+ data.gov.in when keyed) + CAMS at receptors → `air_obs` |
| `met` | 3 h | Open-Meteo multi-location grid, levels 10 m/950/925/850 hPa + boundary layer → `met_grid` |
| `smoke` | after `met` | forward run from recent fires and fresh scars; back runs for receptor cities → `smoke_run` |
| `sentinel` | 6 h | STAC search new clear scenes over AOI tiles → scars, crop state, fire matching |
| `risk` | after `sentinel` | harvested-unburnt clusters → risk score |
| `bulletin` | 07:30 IST | agent writes the day's bulletin in en/hi/pa |

## 7. Agent

Gemini via `google-genai`, manual function-calling loop (≤ 8 tool rounds), streaming over SSE. Tools read the DB
(plus on-demand analyses) and return `{data, sources}`; the runner assigns citation markers `[1]…[n]` to sources.
System prompt: answer only from tool results, cite every number, say "not enough evidence" rather than guess, never
blame named individuals, report at village/district level. After the answer a verifier extracts every number and
checks it against tool outputs (tolerant of rounding/units); if any fail, the model gets one revision pass with the
list; the final `verification` event states the result. Model fallback chain and retry policy per
`docs/research/gemini-agent.md`. Every run is stored in `agent_run`.

Tools (11): `get_fire_activity` · `get_satellite_passes` · `get_burn_scars` · `get_case_file` · `get_air_quality` ·
`trace_air_back` · `forecast_smoke` · `get_weather` · `get_burn_risk` · `get_field_brief` (a district's villages
with candidate unseen burns and its watchlist of harvested fields, from `/api/brief/{district}`; only villages with
fields in the risk run's top tenth go under that name) · `show_on_map` (emits a `map` event only).

Follow-ups: `follow_up_of` (an earlier investigation's run id, under 24 h old) gives the model at most two earlier
question/answer pairs as context, without their citation markers or any tool output. They are never evidence: the
verifier checks the new answer against this run's tool results only, so every figure is fetched again. The `run`
event stores the link and the exact context. `GET /api/agent/stats` counts how often the verifier and the honesty
rules passed, corrected or rewrote runs, from every stored run.

## 8. Robustness

- httpx timeouts (connect 10 s, read 30 s), 3 retries with backoff, per-source cooldown after repeated errors.
- Rate limits respected: FIRMS 5000 tx / 10 min, Open-Meteo free tier, CelesTrak caching (≥ 2 h), OpenAQ key.
- API responses cached briefly in-process (10–60 s) and with HTTP cache headers; imagery cached on disk forever.
- Agent: per-IP rate limit, model fallback on 429/503, cached answers for identical questions within 10 min.

## 9. Frontend (apps/web, Next.js 16 — read `node_modules/next/dist/docs/` before writing code)

Routes: `/` landing (design: `.taste-skill/direction.json`, notes and copy deck in `.taste-skill/`),
`/live` mission control (app style `.taste-skill/app/direction.json`), `/case/[id]`, `/bulletin`, `/method`.
API base URL from `NEXT_PUBLIC_API_URL`. Server components fetch with short revalidation for first paint; client
components poll with SWR. Map: react-map-gl (MapLibre) + deck.gl overlay: fires, scars, risk, smoke trails
(TripsLayer), wind streamlines, AQ stations, satellite ground tracks and swaths (satellite.js from `/api/orbits/tle`),
Sentinel-2 imagery tiles. Basemap and boundaries per `docs/research/geography-and-basemaps.md`; never show a
national outline that departs from the official Government of India depiction.
