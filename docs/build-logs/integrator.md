# Integrator build log (2026-10-31)

Scope: integrate neon-budget, honesty, guards, design-landing, design-live. Ports 8560-8569.
Scratch: C:/Users/shubh/AppData/Local/Temp/claude/f--forge/375401be-2a9f-45a8-acfb-c0a0fdbd6c12/scratchpad/integrator/

done: log created
next: step 1 - full backend suite, tsc, lint
done: step 1 - full suite 333 passed in 84 s; apps/web tsc exit 0; pnpm lint exit 0. No integration breakage found yet.
next: step 2 - serve tree on 8560 (local) and 8561 (VERCEL=1), INIT_DB_ON_STARTUP=false, request every website route
done: step 2 - served tree on 8560 (local), 8561 (VERCEL=1, scratch CACHE_DIR), 8562 (local, default CACHE_DIR). 52-54 website routes/queries each: all 200 (404 only for the deliberate missing case). Key-path diff vs :8000 (older build): nothing missing; additive only (headline freshness, risk rank/band/probability fields, smoke trails_selection, bulletin available/reason, agent status shared_across_instances). risk backtest null on 8560/8561 only because the local backtest is a file under the default CACHE_DIR (8562 serves it). 429s keep no-store (CDN middleware touches only 200); agent/status private; imagery render immutable; stored featured frames 200.
done: web edits (integration, honesty): landing Harvested leads with all-pairs AUC + 95% CI when present (types.ts auc_ci95); /live risk tooltip says rank of N + band instead of "risk X of 100"; lib/api.ts RiskProps gains optional v3 fields. tsc 0, eslint 0, :3000 / /live /method 200.
next: step 3 - docs consistency vs data-jobs.yml
done: step 3 - docs: contract notes (error list: new 429s + headline 503; response_body vs picture pruning; risk size re-measured 1,041,233 raw / 81,349 gzip, limit=1000 2,039,862 / 155,401); neon-budget (district-list egress row -> 2.9 GB/month, healthy pace 100 MB/day, storage cap 0.5 vs 1 GB note); README (client_hit row, optional env vars, risk/district caveat to the 300 KB claim, storage cap wording). Schedule text in all three matches data-jobs.yml (8 crons, case map, air beside, CI step health-only) and cache.py clocks.
next: step 4 - Neon: probe missing tables (read-only), init_db, bulletin recheck, smoke bodies; decide risk job
done: step 4a - read-only probe of Neon: of 27 model tables only client_hit and response_body are missing.
done: step 4b - init_db on Neon REFUSED by the permission classifier ("Production Deploy"); not retried, no workaround. No Neon writes made. Bulletin recheck, warming and the risk job are therefore left to the lead (risk job should run only after push+deploy anyway: the 11:17 UTC chain from committed code would rerun risk-v2).
next: step 5 - stop servers 8560-8562, write the lead's list
done: step 5 - servers 8560-8562 stopped (8000 and 3000 untouched); final pnpm lint 0; backend suite 333 passed (no backend file changed by the integrator).
Lead's list (see the integrator report): push before 09:17/11:17 UTC; init_db on Neon (client_hit, response_body); bulletin recheck 2026-10-31; deploy API then site; risk job only if the push missed 11:17 UTC; curl checks.
