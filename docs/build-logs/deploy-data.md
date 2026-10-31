# Deploy data build log (Neon free tier), started 2026-10-31 05:30 IST

Progress lines: `done:` what is finished, `next:` what comes after.

- done: read architecture, contract notes, API README, integration log. Measured the local DB
  (1,035 MB) and Neon (empty schema + PostGIS: 15 MB).
- done: baselines captured before any code change (old code, local legacy rows): /api/risk (default,
  district=Sangrur, limit=50&min_score=60, bbox, limit=5000), /api/stats/headline over HTTP :8000
  and in-process, plus the agent's get_burn_risk tool output (default, Sangrur).
- done: boundary tolerance test (fire_detection relabelled in a TEMP table, 11,730 rows):
  0.0001 deg -> 0 changes; 0.00015 -> 1 Pakistan point labelled; 0.0002 -> 2 Pakistan points;
  0.0003 -> 3 Pakistan + 2 Punjab; 0.001 -> 6 Pakistan, 14 Punjab, 1 Haryana, 2 UP, 1 HP.
  The Pakistan border is the fragile edge, so it will be kept exact (see below).
- next: risk compaction (risk_run table, compact rows, re-hydration in the router).
- done: risk compaction. analytics/risk.py: run_constants / row_values / expand_factors (13 numbers
  per field; timing re-derived from the harvest dates and the run's as_of). New table risk_run
  (models/risk.py) holds the run's shared values and its backtest. jobs/risk.py: store_scores()
  COPYs into a temp table and one UPDATE ... FROM rewrites only rows whose numbers changed; VACUUM
  (ANALYZE) when > 10% changed. routers/risk.py re-hydrates (legacy rows pass through). Round trip
  over all 172,130 local rows: 0 mismatches, byte-identical orjson output; 1,071 -> 72 bytes of JSON text.
- done: boundaries stored simplified (sources/boundaries.py simplify_area, 0.0002 deg) with India's
  outline kept exact within 0.01 deg inside the wind box. Re-label test over 11,730 detections:
  0 changes in every state and 0 in Pakistan at 0.0002 (0.0003: 2 Punjab; 0.0005: 3 Punjab, 1 UP).
  Geometry 37 MB -> 11 MB.
- done: jobs/prune.py (daily, cron 12:00 IST; PRUNE_DRY_RUN=1 counts only); workflow "30 6 * * *"
  group gains `prune`. Local dry run: 159 of 185 smoke_run rows (18.8 MB) would go.
- done: infra/load_neon.py written.
- next: run the load against Neon; JSON diffs; endpoint comparison; jobs on Neon; egress audit; tests.
- done: first load: 326 MB (harvest_cluster 162 MB, risk_factors still 210 B/row as a jsonb array).
  Then: `v` stored as JSON text in a jsonb string (96 B instead of 208 B per row; 19 MB) and no
  spatial index on harvest_cluster.geom (nothing queries it; 7.7 MB). Second load: 296.9 MB in 58 s.
- done: JSON diffs (generated_at ignored). Old code on local legacy rows (:8000, now) vs new code
  on Neon compact rows (:8400): /api/risk default, district=Sangrur, limit=50&min_score=60, bbox,
  limit=5000 all IDENTICAL; /api/stats/headline differs only in as_of / window / gk2a since (the
  request time). Agent get_burn_risk (default, Sangrur): new code local == new code Neon,
  IDENTICAL; against the 05:30 old-code capture only sources[0] differs (a newer stac_e84 fetch
  landed in the local DB meanwhile; the same change shows in old-code-then vs old-code-now).
- done: every GET endpoint of the contract notes on :8400 (new code, Neon) against :8000 (old
  code, local): 45 requests, all 200 on both; same shape and the same top-level numbers everywhere,
  identical byte sizes except /api/geo/boundaries (1,370,856 -> 1,374,668 B: simplified polygons,
  same count), the MVT tiles (simplified polygons) and /api/bulletin/latest (+`available`, the
  other agent's new field in the working tree). Cold latency from this machine to Neon is
  0.5-5 s (headline 5.0 s); that is the round trip from here, not the queries. Servers stopped.
- next: jobs against Neon (firms_nrt, creams_daily, air, risk) with timings.
- done: jobs against Neon from this machine (`--no-chain`): firms_nrt ok 18.9 s, creams_daily ok
  4.3 s, air ok 289 s (205 s against the local DB: OpenAQ calls dominate).
- done: risk against Neon, first run (old neighbourhood SQL): fields loaded and the neighbourhood
  query done after 4 min 53 s; run 2 rewrote 58,586 of 172,130 rows (34%; VACUUM followed).
- done: neighbourhood() rewritten set-based (window's fires/scars copied once into indexed
  geography temp tables; one lateral pass over scars for count + hectares). On the local DB:
  identical results on 5,063 fields at two dates (0 differences), 2.7x faster; all 172,130 fields
  in 68 s.
- done: tests/test_compaction.py (8 tests: recorded-row round trip, fresh-score round trip with
  missing factors, legacy pass-through, store_scores changed-only + route re-hydration, outline
  kept exact with no point 5 m outside covered, measured tolerance, prune dry run + delete rules,
  load_neon target refusal). Prune test runs only on the *_test database.
- done: risk against Neon, timed from this machine (GitHub's runner will be similar: the time is
  Neon round trips and PostGIS work, not local CPU):
  | run | code | total | scoring part | rows rewritten |
  | --- | --- | --- | --- | --- |
  | 2 | old neighbourhood SQL | 1,203 s | 4 min 53 s | 58,586 / 172,130 |
  | 3 | set-based neighbourhood | 609 s | 1 min 45 s | 56,195 / 172,130 |
  Backtest identical on all three runs (headline 43REP AUC 0.687; 148 pairs, weighted 0.556).
  Advisory lock untouched (run_job). Under the 25 min line with room to spare.
- done: Neon size after the load 296.9 MB; after two risk runs 348 MB (harvest_cluster 133 -> 167
  MB: a run rewrites ~1/3 of the rows, VACUUM makes the old versions reusable, so it settles there;
  n_dead_tup 0). Prune dry run on Neon: nothing to delete yet (all data < 30 days, smoke/met already
  trimmed by the load). The deleting run was not executed on Neon from here; it runs daily in CI.
- done: egress audit (row bytes the SQL returns per cold request, measured on Neon with
  PGresult value lengths; response = uncompressed JSON):
  | endpoint | DB bytes | response |
  | --- | --- | --- |
  | /api/smoke/forward | 12,463,049 | 12,444,189 |
  | /api/geo/boundaries (district) | 3,979,072 | 1,374,668 |
  | /api/geo/boundaries?level=subdistrict | 3,616,798 | 1,145,486 |
  | /api/risk?limit=5000 | 3,349,398 | 9,064,303 |
  | /api/geo/boundaries?level=state | 2,876,760 | 851,731 |
  | /api/scars (default limit 5000) | 2,591,573 | 4,546,079 |
  | /api/smoke/back?city=Delhi | 1,366,563 | 1,364,849 |
  | /api/stats/headline | 689,905 | 10,717 |
  | /api/risk?district=Sangrur | 590,735 | 993,487 |
  | /api/risk (limit 500) | 553,362 | 956,327 |
  | /api/smoke/validation | 474,057 | 430,653 |
  | /api/fires?hours=720 | 452,265 | 1,491,930 |
  | /api/scars/{id} | 363,179 | 8,462 |
  | /api/scars?limit=500&min_confidence=0.5 | 359,583 | 557,512 |
  | /api/smoke/receptors | 284,808 | 16,142 |
  | /api/geostationary/diurnal | 219,751 | 43,346 |
  | /api/air/latest | 154,906 | 134,685 |
  | /api/geo/tiles/7/91/52.pbf | 138,981 | 69,485 |
  | /api/agent/examples | 90,300 | 12,593 |
  | /api/orbits/validation | 61,186 | 4,263 |
  | /api/met/now?city=Delhi | 57,234 | 1,620 |
  | every other GET | < 30,000 | |
  Local (legacy rows, full polygons) for comparison: /api/risk?limit=5000 8.1 MB, /api/geo/boundaries
  9.9 MB. Over ~2 MB and not in my files: smoke/forward (smoke router: serve a job-written body),
  geo/boundaries (geo router: simplify in SQL, e.g. ST_CoverageSimplify, or read a body the
  boundaries job writes), scars default (scars router). /api/risk?limit=5000 (mine) pulls only
  the 5,000 features asked for; its in-process cache is now 600 s (scores change every 3 h).
- done: full suite 236 passed.
