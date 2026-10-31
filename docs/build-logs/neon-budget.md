# Neon budget build log (2026-10-31)

Task: make production (Vercel API + GitHub Actions jobs) fit Neon's free plan (100 CU-hours,
5 GB egress a month) for the rest of the burning season with a wide margin, by measurement.
Scratch: C:/Users/shubh/AppData/Local/Temp/claude/f--forge/375401be-2a9f-45a8-acfb-c0a0fdbd6c12/scratchpad/neon-budget/

Progress lines: `done:` what is finished, `next:` what comes after.

- done: read architecture.md, api-contract-notes.md, deploy-data/serve/api logs, data-jobs.yml,
  api/cache.py, models/assets.py, the website's API calls (apps/web/src).
- next: measurement harness (DB bytes returned + DB-active time) on Neon for the website's routes
  and each CI group's jobs.
- done: harness `scratchpad/neon-budget/meter.py`: an in-process TCP forwarder in front of Neon's
  direct host (client uses `options=endpoint=<id>`), counting every byte Neon sends (TLS records
  included: what Neon bills as egress) and the time of each chunk (DB-active span, 5-min gaps),
  plus a SQLAlchemy hook summing PGresult value bytes. Modes: `routes <list.json>` (TestClient,
  in-process caches cleared before each route) and `job <name> [--no-chain]`.
- done: job_run on Neon before measuring: no CI runs (no `schedule` trigger); last run featured
  (cli) 01:07Z; no other session in pg_stat_activity at 02:0xZ.
- done: Neon compute: max_connections 901 = the 2 CU setting, so autoscaling tops out at 2 CU;
  neon.max_cluster_size 1024 MB; database 332 MB.
- done: routes with the original code on Neon (53 GETs, 55.2 MB wire in total), website queries
  (wire bytes = Neon egress): smoke/forward?max_trails=600&stride=2 12.51 MB; geo/boundaries
  ?level=state&simplify=0.02 2.89 MB; scars?limit=2500&min_confidence=0.5 1.67 MB (+until 1.70 MB);
  smoke/back?city=Delhi 1.37 MB; risk?limit=1000 0.96 MB (not mine); stats/headline 0.85 MB;
  scars/{id} 0.55 MB; smoke/validation 0.48 MB; geostationary/diurnal 0.29 MB; smoke/receptors
  0.29 MB; risk?limit=1 0.24 MB (not mine); air/latest 0.22 MB; everything else < 0.1 MB.
  Per-statement: headline = smoke back summaries of all 24 cities (285 KB, for Delhi's 6 fields)
  + air_latest stations (152 KB, for Delhi only) + geo_fire_slot rows with their stats jsonb
  (160 KB) + polar passes (53 KB); scars/{id} = 72 whole 10 m met grids (534 KB) for one point;
  boundaries = full-resolution WKB, simplified in Python.
- done: codecs on the bodies: xz (lzma preset 6) beats gzip-9 by 30-45 % on GeoJSON (scars 2500:
  2.36 MB raw, gzip 332 KB, xz 223 KB; smoke forward 600/2: 1.90 MB, 393 KB, 264 KB; back Delhi
  1.36 MB, 259 KB, 173 KB); decompression 10-85 ms. Stored bodies use xz.
- done: Vercel docs (cdn-cache, 2026-09-14): the CDN cache is segmented by region (a response is
  cached in the region it was requested from); Accept-Encoding is in the cache key; stale-if-error
  is supported; `private` is never cached.
- next: jobs on Neon (orig code, running in the background); code changes.
- done (prior run, uncommitted): api/cache.py stored-body layer (stored_response, store_payload,
  code_version, canonical_key, job_stamp) and CDN lifetimes per cadence; models/assets.py
  ResponseBody table (separate from rendered_asset) with load_body/save_body/prune_bodies.
  No router uses the stored layer yet. Jobs measured on orig code: firms_nrt 93 KB down / 24 s,
  creams_daily 23 KB / 7 s, air 201 KB / 214 s (scratch jobs_before.jsonl); geostationary was cut off.
- resumed 2026-10-31 06:12Z by a new agent. next: read routers/jobs, fix test_deploy_serve, wire
  stored bodies into smoke/scars/geo, SQL aggregation in headline and scars/{id}.
- done: cache lifetimes: CDN expiry on per-group clocks aligned to the cron schedule (HOURLY :30,
  SIX_HOURLY :15 after met, DAILY 13:15/14:15, BULLETIN 02:15-07:15, WEEKLY daily 03:30), each with a
  late-start catch point; agent/status private; Vary once. tests/test_deploy_serve.py updated
  (13 pass), incl. a test that every clock point follows a cron in data-jobs.yml.
- note: Bash heredocs here eat backslashes (\b became a backspace); use Edit/Write for code.
- done: scratch DB blindspot_nb (copy of local dev DB, harvest_cluster data excluded) for
  old-vs-new identity checks without touching the shared dev DB.
- next: smoke router stored bodies + receptors in SQL; smoke job pre-stores bodies.
- done: stored bodies wired: smoke forward/back/validation (key = run id, stamp = code hash),
  scars list (stamp = job_run of sentinel+boundaries + code; sources live), geo/boundaries (stamp
  = table version + boundaries job + code + shapely/GEOS). smoke job pre-stores forward (600/2 and
  full) and every city's back (default) after writing a run, values rendered as jsonb text locally
  (cache.jsonb_text) and checked against md5(col::text); smoke_validate pre-stores validation.
- done: SQL slimming: receptors + headline Delhi smoke (summary->fields), headline Delhi air
  (air.air_city: one city's stations), diurnal (slot columns without stats; passes as distinct
  satellite+time rows), scars/{id} weather (4 corner cells per hour via SQL, _Corners).
- done: met KEEP_RUNS=1 and in-process grid hand-off to smoke (jobs.met._STORED); fill_admin IS
  DISTINCT FROM; prune job prunes response_body (3 days unused); load_asset reads bytea binary.
- done: headline freshness{job,last_success_at,last_run_at,newest_data_at,stale_after_hours,stale}
  per block; a block that failed -> Cache-Control no-store.
- done: identity proof on scratch DB blindspot_nb, frozen clock (scratch compare.py): 28 routes,
  old (HEAD) vs new live AND new stored: byte-identical except generated_at; headline differs only
  by the added freshness objects.
- next: tests for each behaviour, full suite, then Neon: create response_body, meter routes.
- done: stamps for scars and boundaries switched to cache.table_stamp (count, max and sum of xmin:
  any committed change, by a job or by hand) instead of job_run; geo in-process key follows it.
- done: tests/test_neon_budget.py (13 pass): jsonb_text vs Postgres, stored bytes == live bytes,
  live sources, damaged rows, stamp changes, prune isolation, smoke job pre-store == route bytes,
  receptor SQL fields, met hand-off arrays equal load_level, case-file corners == whole grids,
  freshness, headline no-store on a failed block, air_city == air_latest, fill_admin no rewrite.
- next: full suite; then Neon (create response_body only; meter new code); schedule + docs.
- done: receptors/back_summaries read each distinct sources list once (md5 in SQL).
- done (Neon, read-only, store disabled, new code): headline 854,791 -> 192,288 B; scars/14486
  551,624 -> 34,730; smoke/receptors 287,399 (303,267 old code now) -> 19,507, old==new bytes on
  Neon data; geostationary/diurnal 287,721 -> 37,736; air/latest 222 KB unchanged (<300 KB).
- blocked: creating response_body on Neon was refused by the permission classifier (production
  schema change): the lead must create it (notes). Stored-path sizes measured on blindspot_nb.
- measured: smoke job's met re-read = 2.5 MB/run (bl u,v of 2 members, text) -> 0 when chained.
- next: data-jobs.yml schedule (overlap wakes), clocks, docs/neon-budget.md, README, contract notes.
- done: schedule (data-jobs.yml): every non-hourly group now starts at :17 with an hourly-group run
  (met 17 3,9,15,21; sentinel 17 11; backfills 17 6; bulletin retries 17 3,6; boundaries 17 3 Mon),
  bulletin main 0 2 only; timeout expression and case mapping follow; YAML validated; clocks moved
  (SIX_HOURLY :45, BULLETIN :30 of 2,3,4,6,7); test checks every cron has a case and the timeout
  names a slot. NOTE another agent added scheduler.yml + slot dispatch to data-jobs.yml meanwhile;
  timetable.py reads the cron strings, so the new times propagate.
- done: WKB read in binary (cache.fetch_binary) in smoke.load_labels (2.44 MB hex/run) and the
  geo boundaries live path; identity re-checked: 54/54 byte-identical, headline additive only.
- measured (scratch DB): smoke job alone reads 5.66 MB/run: met grids 2.51 MB, label WKB 2.44 MB
  (hex), fires 128 KB, scars 283 KB.
- next: docs/neon-budget.md, README, contract notes; final suite.
- measured (scratch DB, new code): met chained -> smoke: 1,915,033 B from the DB in 117 s, no met_grid
  re-read, 24 answers stored, no md5 mismatch.
- done: docs/neon-budget.md (per-route and per-job tables, schedule, clocks, projection 45-55
  CU-h and ~2.6 GB a month with the stated traffic, console watch list); README and contract
  notes cadence + caching sections; headline freshness documented.
- done: full suite 297 passed, 1 failed: tests/test_agent.py::test_a_silent_stream_hits_our_deadline
  _and_falls_back (agent/runner.py, another area's uncommitted change; persists alone).
- left: scratch DB blindspot_nb in the local Docker (copy of dev data; drop when done:
  `docker exec blindspot-db dropdb -U blindspot blindspot_nb`). No servers were started.

## Repair (2026-10-31, after review)
- next: (1 high) code_version must hash CRLF-normalised source so the API (Windows bytes copied
  to Vercel) and the GitHub job (LF checkout) agree; (2 high) replay `until=` scars: store per day
  or serve from one stored body; (3 med) clamp/normalise query params onto stored keys + doc;
  (4 med) headline outage: 503 when every block fails; (5 med/low) projection: runner durations,
  31 clock wakes, autoscale arithmetic, CI headline step; (6) robots.txt not mine (main.py).
- design (repair): (1) code_version hashes source with CRLF -> LF; (2) scars stamp =
  content_stamp over the rows the query selects (md5 of rendered columns in SQL), so a replay day
  stays valid while its own rows are unchanged (sentinel's match_fires rewrites matched_at on every
  row daily, so xmin stamps change daily); (3) load_body sends the body only when the stamp matches;
  smoke variants derived from the stored full body; novel heavy keys rate-limited per instance;
  (4) headline: every block failed -> 503 (stale-if-error), partial -> s-maxage 60; (5) clocks all
  on HOURLY :30 points except bulletin 02:30; workflow runs air beside the others; no headline curl.
- next: implement cache.py + assets.py.
- done (repair): code_version normalises CRLF (cache.source_text); probe_body sends body only on a
  stamp match; novel-key limiter (cache.NOVEL_LIMIT 12 / 10 min / instance, guard= on scars
  limit>200, whole-level boundaries, smoke forward variants); scars stamp = content_stamp over the
  query's own rows/columns; smoke variants derived from the stored full answer (variant_run; back
  72 h pass-through re-rendered by jsonb_text and accepted only if it is the stored text); headline
  503 + no-store when every block failed, `public, max-age=30, s-maxage=60` when some failed, and the
  CDN middleware keeps a route's own s-maxage; clocks: SIX_HOURLY :30 of 0,4,6,10,11,16,17,22, DAILY
  13:30/14:30 (21 refresh points a day); workflow: air runs beside the other hourly jobs, CI step
  curls /api/health only. Tests: test_neon_budget + test_deploy_serve 34 passed.
- next: time content_stamp on real data; Neon read-only checks; docs projection; full suite.
- measured (scratch DB, new code, meter2 local): forward?max_trails=598&stride=2 novel 1,681,458 B
  (derived from stored full; was 12.46 MB), repeat 294,751; back?city=Delhi&hours=71 181,093 (was
  1,367,491); back stride=2 181,093; scars limit=2498 1,661,627 (live, counted as novel);
  boundaries state simplify=0.022 4,339,651 (live, counted); replay until=2026-10-11 448,425 live
  then 63,791 stored; whole 47-day replay from store 4.54 MB xz. Identity: 35 routes x2 (live and
  stored) vs HEAD: 68/70 byte-identical, headline only + freshness. Scars content stamp on Neon
  (read-only): 1.0 s full list, 0.19 s until=10-15; Neon burn_cluster stamp equals the copy's.
- runner timing (GitHub API): only 5 data-jobs runs exist; hourly group 9.0 min sequential.
- next: docs/neon-budget.md sections 1-6, then full suite.
- done: docs/neon-budget.md (repair table, replay + invented-variant rows, runner timing, clocks,
  projection 55 / 66 CU-h at 0.25 CU with autoscale arithmetic, egress 2.8 GB, abuse bound, console
  watch list incl. response_body check); api-contract-notes caching section; README.
- next: full suite.
- done: restored test_cdn_headers_on_real_routes (adapted to the clocks; Vary lists Origin once)
  and test_an_agent_run_in_progress_is_never_cached, which the first pass had dropped; smoke's
  in-process cache never keeps a non-200 (a 429 refusal). Full suite: 320 passed.
- left: scratch DB blindspot_nb (drop: `docker exec blindspot-db dropdb -U blindspot blindspot_nb`);
  no servers left running (meter used port 8506 in-process and exited).
