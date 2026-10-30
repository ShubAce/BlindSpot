# agent module - resume log

## done
- read COMMON, agent task, SYNTHESIS build-time updates, contract (architecture 5/7)
- tests/test_agent.py: 26 pass, 1 fail (test_bulletin_replay_checks_every_language)

## next
- fix bulletin replay test; inspect stored agent_run/bulletin rows; SSE e2e; verifier/cache/budget checks; job run
- FIXED test_bulletin_replay: fake client detected English turn as "pa" (prompt mentions Punjabi) -> match "Translate into Punjabi"; made mid-stream 503 replay wait until both translations started (as live); Hindi removed-sentence assertion now expects Devanagari digits (code localises digits). 27/27 pass.
- FIXED: answer cache (runner._RECENT_SQL) and /api/agent/examples now skip runs that cite nothing (stored run 4b5596.. was an all-tools-404 "not enough evidence" run marked ok+passed). Tests updated. 27/27.
- endpoints checked via TestClient on dev DB: examples, runs/{id}, runs/404, status, bulletin/latest en|hi|pa (all 200; fr 422)
- next: real SSE e2e ask (<=10 Gemini calls total), cache replay, bulletin job for 2026-10-31
- real SSE e2e ask run 1e646f5d (3 Gemini calls): 30 frames, contract event order, revision pass fixed a wrong-source cite, verification 8/8; same question again = cached replay (0 calls, run.cached + done.replayed)
- FIXED verifier: number claims swallowed a following "[n]" (citation blanked to spaces, `\s*` ran over it) -> marker seen as absent -> "uncited" wrong AND the cite-the-right-source check skipped for unit-less numbers. Regression test added. All 5 stored ask answers re-verify OK. 28/28.
- next: bulletin job for 2026-10-31 IST (<=~5 Gemini calls)
- bulletin job ran for real: 2026-10-31 en/hi/pa stored (all verified, scripts ok); chained featured_investigations ran 2 runs. NOTE: the chain pushed Gemini use to 18 calls this session (limit was 10) - 503/504s counted too. Do not run the bulletin job again today.
- FIXED: fire tool output lacked the window as a number -> "last 24 hours [1]" failed source attribution (featured run 2eec4667 failed verification). Added window_hours to get_fire_activity and get_satellite_passes. Re-verify of that answer now 8/8.
- FIXED: Gemini held streams 90-145 s before 503/504 (source_fetch latency). Gateway now has its own idle deadline (AGENT_IDLE_S=35, AGENT_BULLETIN_IDLE_S=60) on first chunk and between chunks -> 504 ServerError -> normal cool+fallback. Test added. 29/29.
- next: hardcoded-data grep, http/provenance check, full suite
- FIXED: script check only looked for the *other* Indic script; live pa bulletin 2026-10-31 had Sinhala letters ("ਗੁਣਵੱතා"). foreign_script now flags any non-Latin/Greek letter, vowel sign or digit outside the expected block. Translation is asked once more when the script is wrong. _written() no longer counts a language with script_ok=false, so the next hourly bulletin run (07:30-12:30 IST) rewrites 2026-10-31. Stored rows re-checked: 10-31 pa script_ok=false (true defect), 10-30 pa corrected to true (old flag was the danda false positive).
- tests: agent 31/31; rest of suite 179/179.
## status: DONE (pending: 2026-10-31 pa rewrite by the scheduled job; no more Gemini calls from this session)
