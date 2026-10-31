# Deploy-api build log (2026-10-31): the full API live on Vercel against Neon

Task: merge the two production changes (deploy-data, deploy-serve), bring Neon's schema and data
up to date, run `featured` against Neon, deploy the full bundle to Vercel, verify from outside.

## Progress

- done: read architecture.md, api-contract-notes.md, services/api/README.md, integration.md,
  deploy-data.md, deploy-serve.md, infra/deploy_vercel_api.py.
- next: full test suite with both changes in the tree.
- done: full suite with both changes in the tree: 236 passed (nothing broken by the merge).
- done: Neon schema check (scratch script: every table/column of blindspot.models against
  information_schema on Neon's direct host): no missing table or column; rendered_asset (153 rows,
  copied by load_neon.py) and risk_run (ids 1-3, all with a backtest) are there. The load ran after
  both schema changes, so no re-load (a re-load would also throw away the risk runs made on Neon).
- done: fix where the two changes met: jobs/risk.py prewarm_case_chips now skips chips already in
  rendered_asset (refresh_featured stores the same 144), so a GitHub runner (empty disk) no longer
  reads ~5.8 MB back from Neon on every featured run. Test: test_chip_prewarm_skips_chips_the_store_already_has.
- done: `jobs run featured --no-chain` on Neon (it runs init_db first): ok in 7.0 s; frames fresh
  (built 2026-10-30T23:26Z), 8 frames + 144 chips + manifest stored, 0 missing, 0 pruned;
  case_chips: 144 stored, 0 rendered. rendered_asset 153 rows, 7.76 MB. pg_database_size 348.2 MB.
- next: deploy the full bundle to Vercel.
- BLOCKED: `uv run python ..\..\infra\deploy_vercel_api.py` was refused by the permission
  classifier ("Production Deploy") before it ran; nothing was sent to Vercel. Per the task, no
  workaround. The live URL still serves the old foundation-only build.
- next: everything that does not need the deploy: a Vercel-like local instance against Neon
  (empty CACHE_DIR, INIT_DB_ON_STARTUP=false, the deploy's CORS settings) checked like the live
  URL would be; README deployment section.
- done: found a deploy blocker while preparing the checks: the featured manifest is stored with
  absolute picture URLs of the host that built it (Neon's copy: http://localhost:8000/... in all 8
  frame URLs), and the deploy script never set PUBLIC_BASE_URL, so the live landing hero would
  have pointed browsers at localhost. Fixes: GET /api/imagery/featured serves frame URLs on this
  deployment's PUBLIC_BASE_URL (imagery._rebased; identity on :8000), and deploy_vercel_api.py sets
  PUBLIC_BASE_URL (new --public-url, default https://blindspot-api-azure.vercel.app; the scars case
  file chip URLs and the imagery tilejson URLs are built from it at request time). No other stored
  row on Neon embeds localhost (agent_run, bulletin checked). Test:
  test_featured_frame_urls_point_at_this_deployment.
- done: deploy_vercel_api.py: vercel.json `"regions": ["sin1"]`. Neon is aws ap-southeast-1
  (Singapore); the current live function answers from iad1 (`X-Vercel-Id: bom1::iad1::...`), so
  every database round trip would cross the Pacific (~220 ms). Hobby allows one region.
  Checked by assembling the bundle locally (no Vercel call): vercel.json has the region, 78 python
  files, 27 pinned requirements incl. pillow 13.0.0, rasterio 1.5.2, affine 3.0.1.
- done: api/cache.py: `Vary` no longer lists Origin twice (CORSMiddleware already adds it when the
  request has an Origin; seen as `Origin, Accept-Encoding, Origin`).
- done: Vercel-like instance on :8410 (Neon direct host, empty CACHE_DIR, INIT_DB_ON_STARTUP=false,
  ENABLE_SCHEDULER=0, the deploy's CORS settings, PUBLIC_BASE_URL=http://127.0.0.1:8410), checked
  with the same script the live check will use (scratchpad deployapi/verify.py, output
  deployapi/verify_8410.txt), reference :8000 (local DB):
  - 46 GETs (every contract GET, plus boundaries state/subdistrict, smoke max_trails/stride,
    bulletin hi/pa): all 200. Same numbers as :8000 on 33; the rest are snapshot drifts where Neon
    is newer (its jobs ran later): fires 24 h 236 vs 232, season 4,786 vs 4,782, Delhi PM2.5 51.0
    (31 stations) vs null, air fresh 105 vs 0, geostationary polar passes 22 vs 20, agent examples
    3 vs 4.
  - Largest bodies on the wire (gzip): smoke/forward 2,480,372 (raw 12.4 MB), scars default
    616,369 (raw 4.55 MB), boundaries 382,628. All under 4.5 MB.
  - Featured: 4 frames, stale=false, all 8 URLs on the instance's own base (rebase works), all 200
    JPEG from rendered_asset with the immutable header. A case file outside the stored 24 (42623)
    rendered its chips live in 1.7-10.5 s.
  - Error paths: 10/10 as specified (404 smoke city, 422 region, 404 scar, 400 bbox, 422 lat, 422
    collection, 404 run id, 422 lang, 404 air city, 404 GK-2A slot), JSON error shape, no
    Cache-Control.
  - CORS: https://blindspot-web.vercel.app and http://localhost:3000 echoed on GET and on the
    POST preflight; https://evil.example gets no ACAO (preflight 400).
  - Cache-Control per contract (60/600, 300/3600, 3600/86400; pictures immutable; health none;
    agent/status max-age=10 only).
  - SSE: POST /api/agent/ask with example 1 ("How many fires did satellites detect in Punjab in
    the last 24 hours, and when is the next satellite pass?"): no stored run within 10 min, so one
    live Gemini run (1 of 2 allowed): gemini-3.5-flash-lite, 23 frames, run > thought > tool_call >
    map > tool_result > thought > answer_delta > answer > verification > done, 16/16 verified,
    first event 0.9 s, done 12.9 s; answer says 236 fires, the same as Neon's headline. The same
    question again replayed: run.cached true, done.replayed true, 23 frames in 471 ms.
  - From this machine (India -> Neon Singapore): process start -> app ready 1.9 s, first
    /api/health 2.1 s, first /api/stats/headline 5.5 s (many round trips), then 1-5 ms in-process.
  - :8410 stopped; :8000 untouched.
- done: README (services/api) sections 4a-4c rewritten: what runs where, loading Neon (and when not
  to re-load), running featured on Neon, deploying, the variables the script sets, post-deploy
  checks; CORS_ORIGIN_REGEX and INIT_DB_ON_STARTUP added to the settings table. Contract notes:
  one additive line (picture URLs use the serving deployment's address).
- done: full suite 238 passed (236 + 2 new). Lint: no new findings in the changed lines.
- next (needs the user's permission): `cd services/api; uv run python ..\..\infra\deploy_vercel_api.py`,
  then `uv run python <scratchpad>\deployapi\verify.py https://blindspot-api-azure.vercel.app
  http://127.0.0.1:8000` and `sse.py https://blindspot-api-azure.vercel.app https://blindspot-web.vercel.app`
  (within 10 min of a stored run it replays; otherwise one live Gemini run). Live state now:
  /api/health 200 after a 10.3 s cold start, every data route 404 (old foundation-only build).
