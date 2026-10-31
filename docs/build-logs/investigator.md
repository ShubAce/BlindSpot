# investigator console build log

Owner: src/components/live/investigator/*. Integration: src/components/live/InvestigatorSlot.tsx (mounts it), live.css (column width rules).

## 2026-10-31
- done: read contract notes (Agent and bulletin), live.md "Investigator contract", map-commands, live-state, live.css, lib/sse.ts, lib/live.ts (useAgentRun), app direction (data-terminal), app-screens.md; probed /api/agent/status, /api/agent/examples (3 verified runs), /api/agent/runs/{id} (events + tool outputs + usage), services/api agent/runner.py + tools.py.
- found: the server replays a question only within 10 min (answer_cache_minutes); the 3 examples are hours old, so POSTing an example question would start a LIVE run. Decision: an example plays its stored run from GET /api/agent/runs/{id} in the browser (free, labelled "Replay of a verified run from <time>"), with "Ask it again live" beside it.
- found: events carry no timestamps; per-tool timing is measured in the browser for live runs only (stored replays show the run's total duration_ms instead).
- found: the verifier may revise the streamed draft: `answer` replaces the answer_delta text; verification.revised + first_pass_unverified say what the first draft got wrong (shown as a statement).
- next: build hook (live stream + stored replay + history), plain-word step descriptions, answer with citations and verification, quota line, deep link ?ask=, mount in the slot.
- done: built src/components/live/investigator/* (Investigator.tsx console, use-investigator.ts hook, model.ts reducer + evidence, describe.ts plain words, Steps.tsx, Answer.tsx, data.ts reads + budget, follow.ts scroll, investigator.css) and mounted it in InvestigatorSlot.tsx.
- done (integration, live.css): with the console the title row keeps the full width ("title title title"; the console starts level with the map); below 1100px the console sits right under the map (title, map, console, strip, side).
- done: examples play their stored run (paced; instant under reduced motion; "Skip to the answer"); a typed or linked question that matches an example plays the stored run too; anything else runs live; "Ask it again live" on replays; server replays (run.cached) labelled with when and the cache window from /api/agent/status.
- done: map follows: `map` events through applyAgentEvent (each with "Show again"), get_case_file opens the case summary at call time, and after the run the scars and strongest fires its tools returned are outlined (framed when the agent did not move the map itself); named fields listed with "open case".
- done: verification statement (all N match / which did not), revised-draft disclosure (first_pass_unverified), bad markers; precision line from /api/stats/headline whenever candidate unseen burns are mentioned; sources with IST fetch times, masked URLs link to the source's docs_url.
- done: budget pill + line from /api/agent/status (open / spent with reset time / cooling / no key: evidence-only answers), retry on retryable errors, stop, session history (sessionStorage), collapse (localStorage), deep link /live?ask= (stripped from the address after reading).
- checks: tsc 0 errors, pnpm lint clean, taste lint --app 0/0, inspect --app 0 FAIL 1 WARN (shell's toggle row edge-text at 390). Screens read at 1440 (idle, mid-replay, done, end, live run, errors with agent endpoints blocked, collapsed), 768 full page, 390 (console, input focus, answer). Deep link with an example question (reduced motion) played the stored run.
- live Gemini questions used: 1 of 3 ("Which Haryana districts had the most fire detections in the last 48 hours, and how many burn scars there have no fire alert?": 15/15 figures verified; it is now a verified example).
- status: COMPLETE for this part.
