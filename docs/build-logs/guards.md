# Guards build log (2026-10-31): abuse guards for the serverless API

Task: per-client investigator limits that hold across Vercel instances, the featured manifest on
serverless (no threads), and the picture endpoints (MGRS tile restriction, bbox snapping, a per-client
limit on live renders). Files: api/routers/agent.py, agent/runner.py (RateLimiter + request plumbing),
api/routers/imagery.py, sources/pcdata.py, models/client_quota.py (new), tests/test_guards.py (new).

## Progress

- done: read architecture.md, api-contract-notes.md, deploy-data/serve/api logs, data-jobs.yml header.
- finding: runner.ask() counted a question with the in-memory RateLimiter keyed on
  request.client.host. On Vercel that is per instance (reset on every cold start) and may be the
  proxy address. Joins of an in-flight run and replays of a stored run were already not counted.
- finding: the website (apps/web/src) builds no /api/imagery/render URL itself (imageryRenderUrl in
  lib/api.ts is defined but unused); every picture URL it loads comes from the API (featured frames,
  case-file chips). sse.ts treats a 429 as retryable and reads `detail`.
- done: models/client_quota.py: table client_hit (scope, client hash, at), client_address() (first
  X-Forwarded-For entry, else X-Real-IP, trusted only when VERCEL is set; IPv6 per /64), salted
  HMAC-SHA256 (CLIENT_HASH_SALT, else derived from DATABASE_URL), hit() under a transaction advisory
  lock per (scope, client). Shared counting on when VERCEL is set (or SHARED_CLIENT_LIMITS=1).
- done: runner.count_question(): in-memory first, then the shared count; a shared refusal takes the
  in-memory hit back (RateLimiter.forget). ask() re-checks the in-flight table after counting.
- done: MGRS tiles derived from AOI_BBOX + 0.25 deg in pcdata (pure-Python UTM; pyproj is not in the
  Vercel bundle): 48 tiles (40 without the margin). UTM vs pyproj on a 30 x 16 grid over zones 43/44:
  max difference 0.10 m. Local dev DB: s2_scene uses 23 tiles, burn_cluster/harvest_cluster/
  rendered_asset items 12 tiles, featured frames 43SER: all inside the derived set.
- done: imagery: render/tiles/tilejson refuse items off those tiles before any upstream call;
  render serves exact hits (disk, rendered_asset) first, keeps exact boxes for current frames and
  case chips, snaps any other box (power-of-two grid ~1/32 of the side), counts live renders per
  client (30/min, 300/day; env IMAGERY_RENDER_PER_MINUTE/_PER_DAY). get_item caches upstream 404s 1 h.
- done: featured: on VERCEL never starts a thread; stale after 30 h (daily job + 150 min chain + slack);
  +age_minutes, +stale_after_hours; no stored manifest on VERCEL -> 503 (no in-request build).
- next: tests, then Neon table, then evidence on a local Vercel-like instance (:8520).
- resumed (2nd agent): reviewed diffs of runner/agent/imagery/pcdata/client_quota/test_guards.
- done: tests/test_guards.py 18 passed as written. Added Cache-Control: no-store on both 429s
  (agent ask, imagery render) so no cache ever keeps a refusal; tests assert it.
- done: full suite 260 passed, 4 failed, none in this area: 3 in test_deploy_serve (CDN rule
  numbers changed by the serve area's cache.py edits), test_agent silent_stream (model-b "daily
  budget used" in the test DB's gemini rows; Gateway code untouched here).
- done: client_hit created on the local dev DB (init_db, missing tables only). Evidence harness in
  scratchpad/guards (launch.sh starts 8520/8521 new VERCEL=1, 8522 HEAD baseline VERCEL=1, 8523 new
  without VERCEL; serve.py stubs the investigation: no Gemini, no agent_run writes).
- done: evidence (evidence.py, evidence2.py), servers 8520-8523 stopped. Asks across 2 instances
  200,200,200,429 (3/min), db count 3, other client 200; 429 Retry-After 51, no-store, API error keys.
  Without VERCEL (uvicorn proxy headers off) 5 spoofed XFF: 200,200,200,429,429, 0 shared rows.
  5 replays 0 counted; 4 concurrent identical asks: 1 run, 1 counted. Foreign 10SEG: 3x 400 in 35 ms,
  0 source_fetch rows. 152 stored URLs (8 frames + 144 chips = every stored row): same bytes as DB
  and HEAD, headers equal except +X-Imagery-Bbox and Vary (serve area de-duplicated "Origin, Origin").
  Renders limit 2/min across 2 instances: 200,200,429; redraw of a drawn box not counted. Featured
  age 412 min: VERCEL stale=false (30 h), local stale=true (6 h); refresh threads: new VERCEL 0,
  HEAD VERCEL 2, local 1. Finding: uvicorn trusts XFF from 127.0.0.1 by default and the Dockerfile
  passes --forwarded-allow-ips='*' (not my file): lead note.
- blocked: creating client_hit on Neon was refused by the permission system (production write).
  Left for the lead: scratchpad/guards/neon_table.py (creates only client_hit unless it is the only
  missing table, then init_db()). Until then the shared count fails open (in-memory limit only).
- done: docs: agent section (per-client limit, what counts, address rules, 429 shape,
  status.per_ip_limits.shared_across_instances) and imagery section (featured age/stale/503,
  tile restriction, exact URLs, snapping + X-Imagery-Bbox, live render limit + 429, tiles not counted).
- done: review fix: a box under 0.0001 deg on its longer side (or collapsing at 6 decimals) is now a
  400 (it reached log2(0) in snap_bbox: a 500); test added. ruff: imports sorted, test lint fixed.
- done: full suite 264 passed, 1 failed: test_agent silent_stream, caused by the test DB holding 16
  model-b gemini rows today (= FALLBACK_RPD 20 x 0.8): the test accumulates rows per run. Not this area.
- open: Neon table (lead), Dockerfile --forwarded-allow-ips='*' (lead).

## Repair (3rd agent, 2026-10-31)

Reviewer findings to fix: (M1) /render ran the burn_cluster case-chip query (50 GeoJSON geoms,
~14 KB) before the live-render limit; (M2) /api/imagery/tiles is an uncounted live-render path;
(L1) item ids with a trailing newline pass the `$` regexes; (L2) docs say forged XFF is ignored
off Vercel without saying uvicorn's proxy headers must be off; (L3) joins are per instance only;
(L4) replays are unlimited and each reads a run's events.
- next: M1 (count before the role query, SQL returns a boolean), L1 (fullmatch), M2 (count live
  tiles under a separate, larger per-client budget), docs for L2-L4, tests, evidence on 8520-8529.
- done: L1 pcdata PC_ID/E84_ID end in \Z. M1 render: a 512 px box (the only size that needs the
  burn_cluster chip query) first peeks the client's limit (RateLimiter.peek: in-memory count plus a
  remembered shared refusal, RateLimiter.block) and the chip query returns only clusters whose
  extent is centred within 1e-4 deg of the box (LIMIT 5). M2 tiles: a tile on disk is served first;
  else peek (before get_item), then counted under scope "tiles" (600/min, 6000/day, env
  IMAGERY_TILES_PER_MINUTE/_PER_DAY). L4: ask() remembers a found stored answer per process for
  up to 120 s (never past its replay window). Shared refusals are remembered for asks too.
- done: tests/test_guards.py 29 passed (7 new tests + autouse memo reset).
- next: evidence on 8520-8529 (chip flood, tiles after a render refusal, newline ids, replays),
  docs (agent + imagery sections), full suite.
- done: evidence (scratchpad/guards/repair: serve.py harness, ev.py, chips_check.py) on 8524/8525
  (VERCEL=1, renders 2/min, tiles 4/min) + 8526; all stopped, client_hit rows deleted (0 left).
  Chip flood 12 x 512 px: 200,200,429x10, 2 chip queries, 0 rows (was 12 x 50 rows). Render race
  20 over 2 instances: 200,200,429x18; then tiles: 200x4, 429x6 across instances, drawn tile still
  200, 6 STAC lookups (4 live + 1 per instance before the refusal was remembered). Newline ids:
  400 x3, 0 lookups, 0 renders. 8 replays over 2 instances: 2 stored-run reads (11,417 B each).
  Dev DB, read only: new vs old chip query on all 20,000 distinct chip boxes: identical answers,
  new returns exactly 1 row each (9.8 MB vs 40 MB in total).
- done: docs agent section (per-instance joins, replay reads, uvicorn proxy-header caveat,
  remembered refusals) and imagery section (peek before the chip query, tile limit, newline ids).
- next: full suite.
- done: full suite 283 passed, 4 failed, none in this area: test_agent silent_stream (test DB's
  model-b gemini rows: "daily budget used (reserve kept)", unchanged cause) and 3 in
  test_imagery_risk (`blindspot.jobs.risk` has no `_evaluate`: the risk area's rewrite in
  progress; still failing on a re-run). tests/test_guards.py 29 passed; ruff: no finding on new lines.
- open: Neon client_hit table (lead; the "tiles" scope needs no schema change), Dockerfile
  --forwarded-allow-ips='*' (lead), cross-instance joins (documented as per instance).
