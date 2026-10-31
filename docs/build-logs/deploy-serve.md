# Deploy-serve build log (2026-10-31): the API as a Vercel function in front of Neon

Task: CDN caching, a rendered-image store in the database, the serverless bundle, payload limits,
cold start, the Punjabi bulletin. Response shapes do not change (additive params/fields only).

## Progress

- done: read architecture.md, api-contract-notes.md, services/api/README.md, integration.md.
- next: baseline import time; then items 1-6 in order.
- done: baseline import: `-X importtime` total 1.59 s for blindspot.api.main; plain import 1.70 s (wall,
  incl. interpreter: `uv run python -c` 1.8-2.3 s). Biggest: fastapi 0.37 s, google.genai 0.24 s
  (agent/runner.py, agent/tools.py), geoalchemy2+sqlalchemy ~0.35 s, shapely+numpy 0.11 s.
- done: 1. CDNCacheHeaders middleware (api/cache.py), wired in api/main.py; CORS_ORIGIN_REGEX setting.
- done: 2. models/assets.py (rendered_asset); pcdata.render reads disk -> DB -> render, `persist=`;
  imagery: frames persist, GET /render writes through only for current frames / case chips,
  manifest in DB, refresh_featured(store_assets=True) stores frames+chips and prunes.
- done: 3. infra/deploy_vercel_api.py: WEB_PACKAGES += pillow, rasterio, affine; sets
  INIT_DB_ON_STARTUP=false and CORS_ORIGIN_REGEX=^https://blindspot[a-z0-9-]*\.vercel\.app$.
  Sizes (linux cp312 wheels unpacked with `uv pip install --target --python-platform
  x86_64-manylinux_2_28`): previous WEB_PACKAGES 141.3 MB; pillow 13.0.0 +19.5 MB; rasterio 1.5.2
  (rasterio 51 MB + rasterio.libs/GDAL 67 MB) + affine/attrs/click/pyparsing +~121 MB; total ~282 MB
  of 500 MB. Decision: ship rasterio. It is imported only inside pcdata._read_band, so cold start
  is unchanged, and it lets an instance draw a picture when PC's pixels fail the gate (the
  28 Oct case) instead of answering 503.
- done: 4. smoke `max_trails` / `stride` (+ `trails_selection` field only when used). Measured
  every default response on :8401 (table in api-contract-notes.md "Serverless deployment"): all
  under 4 MB gzip (max /api/smoke/forward 2.48 MB). Raw > 4.5 MB: /api/smoke/forward 12.4 MB and
  /api/scars 4.55 MB rely on gzip; documented with the smaller parameter sets. No default changed.
- done: 5. Cold start: import 1.51-1.56 s after (1.59 s importtime / 1.70 s plain before); already
  under the 2 s target. INIT_DB_ON_STARTUP=false: process start -> /api/health 2.2 s; first
  /api/stats/headline 590 ms, /api/fires 48 ms. No skyfield loader/ephemeris is loaded at import
  (only sgp4lib.theta_GMST1982). The remaining avoidable cost is google.genai (0.23 s), imported
  at module level by agent/runner.py and agent/tools.py (not this task's files).
- done: 6. Punjabi: prompts.SCRIPT_RULES (Gurmukhi U+0A00-U+0A7F only, digits 0-9, Latin only for
  units/abbreviations/markers), TRANSLATE_TEMPERATURE 0.2; bulletin.translate() retries once on
  the other model of the bulletin chain; U+FFFD also counts as a script defect; a run whose
  English is already written translates only the missing/flagged languages from the stored
  English (translate_stored). GET /api/bulletin/latest gains `available` + `reason`
  (routers/bulletin.py, additive). Before: ?lang=pa returned the flagged text with no
  availability flag (script_ok=false, Cyrillic "тоит" inside a Gurmukhi word).
  Live: `jobs run bulletin --no-chain` at 05:37 IST -> pa only, 2 Gemini calls (gemini-3.8-flash
  503 at stream open, then gemini-3.6-flash): script_ok true, 30/30 verified; the only non-Gurmukhi
  marks are 15 dandas (allowed). Live Gemini calls used in total: 2 of the 6 allowed.
- done: featured job live on the dev DB (`jobs run featured --no-chain`, 4.2 s): frames fresh, so
  assets only: 8 frames + 144 chips + manifest stored (153 assets, 7.76 MB, table 8.3 MB). A
  second instance with an empty CACHE_DIR (:8402) served /api/imagery/featured from the DB in 54 ms
  and 14/14 frames/chips in 7-10 ms each, identical bytes and ETag/X-Imagery-*/X-Fetch-Id/
  X-Rendered-At headers to the :8000 disk copies. The rendered_asset table was created in the dev
  DB by init_db (additive; :8000 untouched).
- done: tests/test_deploy_serve.py (12 tests); tests/test_imagery_risk.py: the immutable header
  assertion now includes s-maxage. Full suite: 228 passed. Test servers :8401/:8402 stopped.
- done: docs/api-contract-notes.md: "Serverless deployment" section; additive notes in Caching,
  smoke, imagery render, bulletin.

## Open / for the next agents

- jobs/risk.py `prewarm_case_chips` (owned by the risk agent) is now redundant: refresh_featured()
  stores the same 144 chips. On a GitHub runner (empty disk) it reads all of them back from Neon
  every featured run: ~5.8 MB x 4 runs/day, about 0.7 GB/month of Neon egress. Drop it, or skip
  chips that `pcdata.stored_keys()` already has.
- GET /api/risk reads its backtest report from CACHE_DIR (analytics/risk.py BACKTEST_FILE). On
  Vercel /tmp is empty, so the backtest block will be missing unless it moves to the database.
- README section 4a (services/api/README.md) still says pillow/rasterio are not shipped and does not
  list CORS_ORIGIN_REGEX; not in this task's files.
- After deploying, check that Vercel's CDN honours `Vary: Origin` (curl the same URL with two
  Origins). If it does not, answer GETs with `Access-Control-Allow-Origin: *` (no credentials are
  used) instead of echoing the origin.
- /api/imagery/featured: when the stored manifest is older than 6 h, an instance still starts a
  background rebuild thread, which Vercel freezes after the response. Harmless while the 6-hourly
  job keeps the manifest fresh.
