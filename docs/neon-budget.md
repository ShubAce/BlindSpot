# Neon budget

BlindSpot's API (Vercel, `sin1`) and its jobs (GitHub Actions) read and write one Neon Postgres +
PostGIS database on the free plan: **100 CU-hours of compute and 5 GB of public egress a month,
1 GB of storage**. If compute or egress runs out, Neon suspends the database until the next month
and the live demo goes blank. This page is the measured budget for the rest of the burning season
(November 2026), what was changed to fit it, and what to watch.

Everything here was measured on 2026-10-31. Neon egress is every byte the database sends to a
client, TLS included. It was counted by a TCP forwarder in front of Neon's direct host
(`scratchpad/neon-budget/meter.py`); compute time is the span between the first and last byte a
job or request exchanges with the database, plus the 5 minutes Neon keeps an idle compute awake.

## 1. What was changed, and why

| Change | Where | Effect |
| --- | --- | --- |
| Stored answers: the finished body of a heavy route is kept in `response_body`, xz-compressed, under a stamp of its data and the code that renders it; a request whose stamp matches reads the stored bytes and sends exactly the bytes a live computation would (only `generated_at` is fresh) | `api/cache.py` (`stored_response`, `store_payload`), `models/assets.py` (`ResponseBody`) | smoke trails, scar polygons and boundaries cost 8-15 % of their rows |
| The smoke job stores the website's smoke answers right after it writes a run, from the values it holds (rendered as Postgres renders jsonb and checked against `md5(col::text)`), so even the first visitor never reads the 12 MB trails column; `smoke_validate` does the same for `/api/smoke/validation` | `jobs/smoke.py` (`store_bodies`) | first request after a smoke run: 263 KB instead of 12.5 MB |
| Aggregation in SQL: the headline reads Delhi's stations and Delhi's smoke summary only; receptor markers pick seven summary fields and read the shared sources list once; GK-2A's diurnal reads the slot columns it uses and one row per satellite pass time; a case file reads the four grid cells around the scar for each hour instead of 72 whole grids | `routers/stats.py`, `routers/air.py` (`air_city`), `routers/smoke.py` (`back_summaries`), `routers/geostationary.py`, `routers/scars.py` (`_grid_corners`) | headline 855 KB to 192 KB, case file 552 KB to 35 KB, receptors 303 KB to 20 KB, diurnal 288 KB to 38 KB |
| Geometry read in binary (a `bytea` in the text protocol is hex, twice its size) | `cache.fetch_binary`: smoke labels, boundaries' live path; `models/assets.load_asset` (stored pictures) | half the bytes |
| The smoke job takes the wind grids the met job just stored from memory | `jobs/met.py` (`_STORED`, `stored_level`), `jobs/smoke.py` | 2.5 MB less per smoke run |
| `KEEP_RUNS = 1` met runs per model (every reader takes the newest; the daily prune kept one anyway) | `jobs/met.py` | storage |
| `fill_admin` rewrites only rows whose state or district changes | `geo.py` | the weekly boundaries job no longer rewrites ~100k unchanged rows |
| Unused stored answers (3 days) are pruned daily | `jobs/prune.py` | storage |
| CDN expiry on clocks aligned to the job schedule | `api/cache.py` (`Clock`, `CDN_RULES`) | one database wake-up refreshes a whole group; nothing is refreshed while its data cannot have changed |
| Every non-hourly job group starts with an hourly-group run | `.github/workflows/data-jobs.yml` | the groups share wake-ups instead of each paying a 5-minute tail |
| Headline blocks carry `freshness` (feeding job, its last success, newest data, `stale`) | `analytics/headline.py`, `routers/stats.py` | a stopped feed shows |

After review (2026-10-31, second pass):

| Change | Where | Effect |
| --- | --- | --- |
| `code_version` hashes source text with line endings normalised (`cache.source_text`) | `api/cache.py` | the API is deployed from a Windows tree (CRLF, `shutil.copy2`) and the jobs run from a Linux checkout (LF): with raw bytes the job's pre-stored smoke answers carried a stamp the API never matched (`8a4760adaa16` vs `91aac64e54d2`), so every cold smoke request was live (13.4 MB) |
| Scar lists are stamped by `cache.content_stamp`: Postgres' md5 over the rows the query selects, in the columns the answer shows | `routers/scars.py` | the sentinel job rewrites `matched_at` on every scar daily (so a table-wide stamp changed daily) and adds newer scars; each season-replay day (`until=`) now keeps its stored answer until one of its own scars changes. `until` on or after today is the plain list |
| A stored row whose stamp is stale is reported without its body (`assets.probe_body`) | `models/assets.py` | a stale check no longer downloads the old body (up to 1.6 MB) to throw it away |
| `max_trails` / `stride` / `hours` variants of a smoke run are derived from the stored full answer (`smoke.variant_run`), rendered by the same functions to the same bytes | `routers/smoke.py` | any forward variant ~1.7 MB instead of 12.5-13.4 MB; any back variant ~0.18 MB instead of 1.37 MB |
| Never-seen heavy variants (`guard=True`: smoke forward variants, scar lists over 200 except the live map's list with one `until` or `district`, whole boundary levels) are limited to 20 per 10 minutes per instance, then 429 | `api/cache.py`, routers | bounds what inventing parameters can cost; the website's own keys are stored and never refused |
| Headline: every block failed (database down) -> 503 + `no-store`, so the CDN's `stale-if-error` serves the last good one; some failed -> `s-maxage=60`, which the CDN middleware keeps | `routers/stats.py`, `api/cache.py` | the front page keeps its numbers during an outage; a failing block no longer sends every poll to Neon |
| All clock points on the hourly :30 points (SIX_HOURLY from :45, DAILY from :15) | `api/cache.py` | 31 -> 21 refresh wake-ups a day at most |
| `air` runs beside the other hourly jobs; the CI step requests `/api/health` only | `.github/workflows/data-jobs.yml` | the hourly wake ~9.0 -> ~6.3 min on the runner; 4.7 MB a day of headline reads gone |

Response shapes are unchanged except the additive `freshness` objects. Identity was proved on a
copy of the local database with the clock frozen: 28 routes (every website query plus the
defaults and variants of each changed route) answered by the old code (HEAD) and by the new code,
live and from the store: **54 of 56 bodies byte-identical except `generated_at`**; the other two
(the headline, live and replayed) differ only by the added `freshness` objects. On Neon's own data, `/api/smoke/receptors` old vs
new is byte-identical too. The tests in `tests/test_neon_budget.py` keep these properties.

## 2. Per-route egress (one cold request)

"Before": original code on Neon. "After": new code. Rows marked *Neon* were measured on Neon
(live path); rows marked *stored* are the stored body (binary read, plus ~0.5 KB of protocol and
stamp queries), measured on a copy of the same data.

| Route (website query) | Before | After | How |
| --- | ---: | ---: | --- |
| `/api/smoke/forward?max_trails=600&stride=2` (live map) | 12,513,734 | 262,924 | stored, written by the smoke job |
| `/api/smoke/back?city=Delhi` | 1,372,654 | 171,716 | stored, written by the smoke job |
| `/api/scars?limit=2500&min_confidence=0.5` (live map) | 1,670,563 | 222,032 | stored; recomputed (1.67 MB) only when a scar it lists changes |
| `...&until=<day>` (season replay, 47 days) | 1,448,829-1,708,800 each on Neon; ~74 MB a Play after each daily update | 63,791-227,993 each stored; 4.5 MB for all 47 | stored per day, valid while that day's own scars are unchanged (`content_stamp`); a never-seen day costs its live read once (2026-10-11: 448,425) |
| `/api/scars?unseen=true&min_confidence=0.9&limit=120` (landing) | 119,647 | 19,496 | stored |
| `/api/geo/boundaries?level=state&simplify=0.02` (case page) | 2,889,943 | 23,728 | stored; first request after a boundary change ~1.45 MB (binary) |
| `/api/stats/headline` | 854,791 | 192,288 | Neon |
| `/api/scars/{id}` (case file) | 551,624 | 34,730 | Neon |
| `/api/smoke/receptors` | 287,399 | 19,507 | Neon |
| `/api/smoke/validation` (method page) | 476,518 | 41,308 | stored, written by `smoke_validate` |
| `/api/geostationary/diurnal` | 287,721 | 37,736 | Neon |
| `/api/air/latest` | 222,305 | 222,382 | Neon (unchanged, under 300 KB) |
| `/api/orbits/validation` | 95,240 | 95,950 | Neon (unchanged) |
| `/api/met/now?city=Delhi` | 63,007 | 62,651 | Neon (unchanged) |
| `/api/fires?hours=24` | 43,693 | 46,341 | Neon (unchanged; more fires) |
| `/api/fires/daily`, `/fires/hours`, `/scars/summary`, `/sources`, `/geo/cities`, `/orbits/tle`, `/met/wind-field`, `/geostationary/latest` | < 21 KB each | same | Neon |
| `/api/risk?limit=1000`, `/api/risk?limit=1` (not in this area) | 960,841, 236,704 | unchanged | see open issues |

Defaults and other variants:

| Route | Before | After |
| --- | ---: | ---: |
| `/api/smoke/forward` (full resolution: the developer `/lab` page and the agent's `forecast_smoke`) | 12,513,844 | 1,555,264 stored (an opt-in heavy variant) |
| `/api/scars` (5,000 polygons) | 3,163,348 | 410,656 stored |
| `/api/geo/boundaries` (districts) | 4,001,285 | 202,164 stored |
| `/api/geo/boundaries?level=subdistrict` | 3,641,805 | 167,764 stored |
| `/api/smoke/back?city=Delhi&max_trails=100&stride=2` | 1,372,632 | 48,240 stored |
| `/api/smoke/forward?max_trails=598&stride=2` (an invented variant; review: 12,464,742) | 12.46 MB | 1,681,458 first (derived from the stored full answer), 294,751 after |
| `/api/smoke/back?city=Delhi&hours=71`, `&stride=2` (review: 1,367,491 each) | 1.37 MB | 181,093 first (derived), stored after |
| `/api/scars?limit=2498&min_confidence=0.5` (invented) | 1.66 MB | 1,661,627 live, counts against the 20-per-10-minutes allowance |
| `/api/geo/boundaries?level=state&simplify=0.022` (invented) | 4.34 MB | 4,339,651 live, counts against the allowance |
| `/api/fires?hours=720` | 733,549 | unchanged (< 1 MB) |

The "after" figures for invented variants were measured on the copy with the repaired code
(scratch `meter2.py`, local target). Old-vs-new identity was re-run after the repair on 35 routes
(the 28 above plus invented smoke variants, replay days, a future `until` and a district list):
**68 of 70 bodies (live and stored) byte-identical except `generated_at`**, the headline differing
only by `freshness`; `tests/test_neon_budget.py` checks the derived smoke variants byte for byte.

Stored answers on the copy: 17 bodies, 3.7 MB in all (the full forward run is 1.56 MB of it).
On Neon expect ~30 bodies, ~5 MB: the smoke job stores 2 forward + 24 back bodies per run, and
the prune job deletes bodies nobody read for 3 days.

## 3. Jobs: egress and database-active time

Durations are `job_run` on Neon over the last 7 days (average / maximum); every one of those runs
was a local CLI run. The GitHub runner is slower for network-bound jobs: the one runner-timed
hourly run took air 368 s and geostationary 106 s (section 4). Egress was measured on
Neon (firms_nrt, creams_daily, air: original code, unchanged since) or on the copy of the data
(smoke).

| Job | DB-active s (avg / max) | Egress per run | Note |
| --- | ---: | ---: | --- |
| `firms_nrt` | 13 / 37 | 93 KB | |
| `creams_daily` | 7 / 12 | 23 KB | |
| `air` | 159 / 289 | 201 KB | many upstream calls, each logged; the compute stays awake throughout |
| `geostationary` | 10 / 14 | not measured (small: one slot) | |
| `met` | 66 / 105 | small (writes) | 61 s pacing between the two Open-Meteo POSTs |
| `smoke` | 35 / 139 | 5.66 MB alone (old); **met + smoke chained, new code: 1.92 MB, 117 s active** (measured) | grids 2.51 MB now from memory; label polygons 2.44 MB hex now ~1.2 MB binary; fires 128 KB; scars 283 KB; the run stored 24 answers (2 forward, 22 cities), every jsonb column matching Postgres' md5 |
| `smoke_validate` | 8 / 15 | ~3 MB (a week of winds and boundary-layer heights) | daily |
| `sentinel` | 972 | not measured | daily, 40-minute budget |
| `risk` (chained after sentinel; not this area) | 344 / 1203 | not measured | the largest unknown |
| `featured` | 49 / 103 | small (pictures written) | |
| `bulletin` + `featured_investigations` | 135 / 263 + 117 | in-process API calls, now slim or stored | |
| `firms_backfill`, `geostationary_backfill`, `prune` | 34 / 86, 176 / 333, short | small | daily |
| `boundaries` | 190 / 236 | small | weekly |

## 4. Schedule (UTC; IST = UTC + 5:30)

| Cron | Jobs | Wake-up |
| --- | --- | --- |
| `17 8-18 * * *` | firms_nrt creams_daily geostationary, with air beside them | 11 a day, ~6.3 min + 5 min tail |
| `17 0,3,6,21 * * *` | the same | 4 a day |
| `17 3,9,15,21 * * *` | met air_dss (-> smoke) | shares the 03:17, 09:17, 15:17, 21:17 wake |
| `17 11 * * *` | sentinel smoke_validate tle (-> risk, featured) | shares the 11:17 wake, extends it ~25-60 min |
| `0 2 * * *` | bulletin (-> featured_investigations), 07:30 IST | its own wake, ~9 min |
| `17 3,6 * * *` | bulletin retries (a no-op once written) | shares the 03:17 and 06:17 wakes |
| `17 6 * * *` | firms_backfill geostationary_backfill prune | shares the 06:17 wake |
| `17 3 * * 1` | boundaries (Monday) | shares the 03:17 wake |

The hourly group used to run its four jobs one after another: 9.0 min on the GitHub runner
(2026-10-31 06:41 run: air 368 s, geostationary 106 s, firms_nrt 23 s, creams_daily 10 s; air waits
on OpenAQ's 60-requests-a-minute limit and data.gov.in). `air` needs nothing the others write, so
it now runs in its own process beside them (`beside` in the workflow's job picker) and the group
takes as long as air, ~6.3 min. geostationary still runs after firms_nrt, whose rows it reads.

Before, met (`47 2,8,14,20`), sentinel (`40 11`), the backfills (`30 6`), the bulletin and its
five hourly retries (`0 2-7`) and boundaries (`0 3 * * 1`) each started outside an hourly run and
paid their own wake and 5-minute tail: ~27 wake-ups a day against 16 now. `scheduler.yml`
dispatches the same cron strings (it reads them from `data-jobs.yml`).

The CDN keeps API answers until the group's data can next have changed (`api/cache.py`):

| Group | Expires (UTC) | Routes |
| --- | --- | --- |
| HOURLY | :30 of 08-19 and 00, 01, 03, 04, 06, 07, 21, 22 | fires, headline, sources, air, GK-2A, scars/summary, orbit passes, agent examples |
| SIX_HOURLY | :30 of 04, 10, 16, 22 (the hour after met), then 06, 11, 17, 00 | smoke, met, stubble share, orbit validation |
| DAILY | 13:30, 14:30 | scars, risk, smoke validation, featured imagery, element sets, finished agent runs |
| BULLETIN | :30 of 02, 03, 04, 06, 07 | bulletin |
| WEEKLY | 03:30 daily | boundaries, places, tilejson |

Every point except the bulletin's 02:30 is also an HOURLY point, so one page view refreshes every
group that has expired in one database wake-up: **at most 21 refresh points a day**
(`tests/test_deploy_serve.py` checks it). Before this repair SIX_HOURLY (:45) and DAILY (:15) had
points of their own: 31 a day. Each group's later point catches a run GitHub started late.
`stale-while-revalidate` (1 day; a week for geography) answers at once with the old copy while the
CDN refreshes; `stale-if-error` (a week) keeps answering if the API or the database fails, for any
5xx: so a headline whose every block failed (the database is down or suspended) is a 503, not a
200 (see `routers/stats.py`). A headline with only some blocks failed is kept 60 s by the CDN.
`/api/health` and `/api/agent/ask` never reach the CDN cache; errors are never cached.
`/api/agent/status` (the Gemini quota, the same for every visitor) is on the HOURLY clock, so the
minute poll of an open `/live` tab is answered by the CDN instead of the database.

## 5. Monthly projection (30 days)

### Compute (CU-hours)

A wake-up costs its active time plus the 5 minutes Neon keeps an idle compute awake. Jobs, with
overlapping groups counted once:

| Part | min/day (typical) | min/day (max) |
| --- | ---: | ---: |
| 15 hourly-group runs: ~6.3 min (air beside the others; runner-measured, see 4) + 5 min; max 9 min + 5 | 170 | 210 |
| met + smoke (their own run, same start; inside the hourly wake unless longer) | 0 | 8 |
| sentinel, risk, featured, smoke_validate, tle after the 11:17 run | 23 | 60 |
| backfills + prune after the 06:17 run (their own run, same start) | 0 | 1 |
| bulletin at 02:00 (own wake) | 9 | 11 |
| **jobs** | **202** | **290** |

Only the hourly group has a runner measurement (the only 5 runs of `data-jobs.yml` so far were all
on 2026-10-31); the other durations are `job_run` from local CLI runs. Network-bound jobs are
slower on the US runner (air: 368 s against 159 s locally); sentinel and risk are dominated by
compute and Sentinel-2 reads, so the "max" column carries their largest local runs.

API traffic assumption (judging month), stated so it can be checked against the console:

- an open `/live` tab costs nothing of its own: every route it polls, `/api/agent/status`
  included, is answered by the CDN between the refresh points below (before 2026-10-31 the status
  poll reached the database every minute: 120 min for a tab open 2 hours a day);
- **every one of the 21 refresh points hit once a day** by a visitor outside job wake-ups (the
  website's server in `sin1` and the browsers in India refresh at the same moments, so they share
  the wake): 21 x 5.5 = 115.5 min;
- **crawlers**: the API has no `robots.txt` (404; `api/main.py` is not this area) and crawlers that
  run the website's JavaScript fetch the API from their own region at their own times: an
  allowance of 3 wake-ups a day, 16.5 min.

That is 132 min a day. Together, at Neon's smallest size (0.25 CU):

| | min/day | CU-hours / month |
| --- | ---: | ---: |
| jobs (typical / max) | 202 / 290 | 25 / 36 |
| traffic | 132 | 16.5 |
| **total** | **334 / 422** | **42 / 53** |

The review's recount of the earlier setup came to ~67 CU-hours (jobs 246 min with the 9-minute
hourly group, traffic 290 min with 31 refresh points). Running air beside the others (-40 min a
day) and putting every clock on the hourly points (-10 wakes, -55 min a day) bring the same
assumptions to **55 CU-hours (55 % of the allowance), 66 if every job takes its longest time on
the same day**. Putting `/api/agent/status` on the hourly clock (it had reached the database on
every poll) brings them to **42 and 53**.

Autoscaling adds `(cap - 0.25) CU x hours at the cap x 30 days` on top. If sentinel and risk lift
the compute to the cap for their 23-60 minutes a day: at a 0.5 CU cap +3 to +7.5 CU-hours, at 1 CU
+8.6 to +22.5, at 2 CU (what `max_connections` 901 shows is allowed today) +20 to +52.5, which
would pass 100 in the worst case. **Set the autoscaling range to 0.25-0.5 CU** (or 0.25-1 CU if
the risk job runs out of memory or time at 0.5). A CPU-bound job costs about the same CU-hours at
any size (half the CU for twice the time); the cap only limits how far a busy hour can spike.

If the console shows more than ~2 CU-hours a day, these levers are ready, largest first: the
afternoon hourly runs every 2 hours (`17 8-18/2`; about -10 CU-hours, fires and air up to 2 h
older on the site), and a `robots.txt` that disallows the API host (the crawler allowance).

### Egress (GB)

| Part | MB a day | GB a month |
| --- | ---: | ---: |
| hourly group: 15 x (93 + 23 + 201 + ~100 KB) | 6.3 | 0.19 |
| met + smoke: 4 x ~2 MB | 8 | 0.24 |
| smoke_validate | 3 | 0.09 |
| sentinel + risk (unmeasured; allowance) | 20 | 0.60 |
| bulletin, backfills, prune | 6 | 0.18 |
| CI check: `/api/health` only (the headline request is gone: it cost 194 KB a run, 4.7 MB a day) | ~0 | 0 |
| API hourly set at 20 points: the browsers' set (~0.55 MB) and the website server's (~0.3 MB) | 17 | 0.51 |
| API smoke, met set (~0.52 MB) x 8 points | 4.2 | 0.13 |
| API scars, risk (1.2 MB), validation, case files: ~1.6 MB x 2 points x 1.5 variants | 4.8 | 0.14 |
| season replay: 2 Plays a day from stored days (47 days, 4.5 MB) + ~3 changed days live (1.6 MB each) | 13.8 | 0.41 |
| boundaries and other geography (incl. `/live`'s label mask `level=state&bbox=68,24,82,36&simplify=0.01`, stored after its first build) | 0.4 | 0.01 |
| `/live` district lists (`scars?district=<name>&limit=2500&min_confidence=0.5`, fetched only on a district click; stored ~0.33 MB, ~1.4 MB to rebuild after that district's scars change): allowance of 2 rebuilds and 4 stored reads a day (integrator's assumption, added after this table was measured) | 4.1 | 0.12 |
| agent questions (20 a day x ~0.5 MB) | 10 | 0.30 |
| **total** | **98** | **2.9** |

**About 2.9 GB a month, 58 % of the 5 GB allowance** (2.8 GB before the district-list row), of which 0.6 GB is an allowance for the
unmeasured sentinel and risk jobs and ~0.4 GB is the risk route (0.96 MB per cold request,
outside this area). An encoding (`Accept-Encoding`) or `Origin` the CDN has not seen at a point
costs one more refresh of that endpoint; the table counts the two variants the website produces.

Abuse bound (not in the total): a client inventing parameters gets at most 20 never-seen heavy
variants per 10 minutes per instance, each at most ~4.3 MB (a whole boundary level at a new
`simplify`; a smoke variant is ~1.7 MB, derived from the stored full answer), then 429. That is a
speed bump, not a wall: for a sustained attack add a Vercel Firewall rate-limit rule on the API
project.

Storage: 333 MB on Neon; stored answers add ~10 MB (the 47 replay days are 4.5 MB). The brief
gives the plan's storage as 1 GB; `infra/load_neon.py` and `services/api/README.md` were sized
for 0.5 GB, which still leaves ~160 MB. Check the cap on the console's Usage page.

## 6. What to watch in the Neon console

- **Billing / Usage**: compute hours and data transfer month to date. Healthy pace: under
  2 CU-hours and 100 MB a day. Over 3 CU-hours or 150 MB a day for two days running: find the
  cause before the month runs out.
- **Compute settings**: autoscaling **0.25 CU minimum, 0.5 CU maximum** (see 5; `max_connections`
  901 shows it can reach 2 CU today); scale-to-zero after 5 minutes stays on.
- **Monitoring**: the compute should be idle most of the night (between the 21:17, 00:17, 03:17
  and 06:17 UTC runs). A compute that never suspends means something polls the database: a
  visible `/live` tab (agent status, every minute), an uptime monitor on `/api/health` (it
  queries the database and is never cached), or a job stuck in a loop.
- **Storage**: `response_body` should stay ~10 MB (`SELECT count(*), sum(octet_length(body))
  FROM response_body`); `smoke_run` and `met_grid` are pruned daily.
- **`response_body` must exist on Neon** (`SELECT to_regclass('public.response_body')`). On
  2026-10-31 it did not: until the first job run of the new code creates it (`init_db`), every cold
  request is live (forward 600/2 13.4 MB, scars 1.68 MB, boundaries 1.45 MB, back Delhi 1.40 MB,
  validation 0.48 MB on Neon). The next hourly run after the push creates it; the next met run
  stores the smoke answers.
- After loading data by hand (`infra/load_neon.py`), stored answers follow by themselves: smoke
  answers are keyed by run id; scar lists by `cache.content_stamp` (the rows each query selects,
  in the columns it shows); boundaries by `cache.table_stamp`.
