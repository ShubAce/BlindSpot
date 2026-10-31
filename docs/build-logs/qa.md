# QA build log (final pre-deploy QA of apps/web)

Owner: final QA across all routes. Edits are fixes only; shared files touched additively.

## 2026-10-31
- done: read build logs landing.md, live.md, investigator.md, pages.md (incl. Critique sections), integration.md tail.
- next: tsc + lint, production build (pnpm build), pnpm start --port 3100 against :8000.
- done: tsc 0, lint 0. `pnpm build` passes (12 s; 8 routes, / /bulletin /case/[id] /live /method partial prerender).
- found: build prints 4x "Unexpected cache miss after cache warming phase" for "/". Traced with NEXT_PRIVATE_DEBUG_CACHE=1: serverGet's short-lived entries (expire 120 s) for headline, featured, risk, sources were already in the in-memory cache handler from /method's prerender in the same worker, so the warm phase resolved them "prerender-dynamic" without saving them to the resume cache; the final phase then misses and Next turns them into dynamic holes, which is exactly the intended outcome (these reads run at request time). Benign, arguments are deterministic; left as is.
- next: pnpm start --port 3100, dogfood.
- found: the API's CORS allows only http://localhost:3000, so a browser on the :3100 production server cannot read the API (every client panel failed). Not an app bug (the deployed API sets CORS_ORIGINS / CORS_ORIGIN_REGEX). Browser checks on :3100 run Chrome with --disable-web-security; noted for deploy.
- done: landing at 1440 on prod: hero crossfade, live pass line (computed in the browser), 6 slips link to /case/{id}, no maplibre/deck chunks, no broken images, no "undefined/NaN/null" text.
- done: /live journey at 1440 on prod: 4 layers on and off (aria-pressed follows), list row 1 -> case summary with Sentinel-2 before/after and "Open the case file", season replay plays (18 Sep: 5 fire detections) and back to Live, example "Why is Delhi's air bad today" replays its stored run with steps, map layer, verified answer and sources.
- fixed: investigator step results printed the API's log notation: a Python dict `{'h24': 43.5, 'h48': 22.0, 'h72': 14.9}`, ISO dates `2026-10-31 02:30` and ungrouped figures `67854`. New `tidySummary` in investigator/describe.ts (notation only, never a value; UTC stamps shown in IST; ids after Case/scar left alone), used by Steps.tsx.
- known (not fixed): MapLibre warns that the OpenFreeMap style asks for sprite images "circle-11" and "wood-pattern" it does not ship; console only, nothing visible.
- done: /case/18905, /bulletin (en, hi, pa tabs update ?lang=), /method, /case/99999999 at 1440 on prod: titles right, no overflow, no bad text.
- fixed (SEO/sharing): favicon was the create-next-app "N" -> app/icon.svg (the BlindSpot mark) + apple-icon.tsx; starter favicon.ico removed (backup in scratchpad). Share card app/opengraph-image.tsx (static, built from repo files: CC BY-SA straw-burning photo with its credit + Anton, OFL, in src/app/_og; no figures). Root metadata: openGraph/twitter card/robots, optional NEXT_PUBLIC_SITE_URL metadataBase (Vercel falls back by itself); root description no longer carries the typed "Satellites look at 1:30. Fires start at 5." (the landing critique had already removed it from the page). app/robots.ts (disallow /lab).
- fixed: unmatched URLs showed Next's default "404: This page could not be found." -> app/not-found.tsx in the site's type with ways in. Added app/error.tsx (no route had an error boundary).
- next: rebuild, restart :3100, check head tags, then Lighthouse.
- done: Lighthouse 12.8.2 on the prod build (simulated throttling, Chrome --disable-web-security for the :3100 CORS). Before: / mobile perf 73 (LCP 7.0 s, TBT 190 ms, CLS 0), desktop 96; /live mobile 33 (LCP 9.6 s, TBT 8.1 s, CLS 0.149), desktop 68 (LCP 0.6 s, TBT 2.4 s, CLS 0.05). a11y/best-practices/SEO 100 on both.
- fixed (perf): the hero downloaded all four Sentinel-2 frames (~930 KB) at once, racing the fonts on a slow link; Crossfade now fetches only the frame on screen and the rest once it has arrived (cycle starts then). / mobile 73 -> 81, LCP 7.0 -> 4.5 s.
- fixed (perf): /live started the MapLibre + deck.gl chunk (~1.8 MB) during hydration; MapPanel now mounts it on requestIdleCallback (timeout 1 s). /live mobile LCP 9.6 -> 6.4 s simulated; with devtools throttling LCP 1.6 s on both / and /live, CLS 0 and 0.047.
- known: /live TBT stays high (devtools-throttled mobile 5.6 s, desktop 2.6 s): evaluating maplibre-gl 6 (1.1 MB) and deck.gl (0.7 MB) after first paint. The landing ships no maplibre/deck (14 JS files, 218 KB).
- done: Lighthouse on /case/18905, /bulletin (en, pa), /method, /case/99999999 (mobile + desktop): accessibility 100 everywhere except /method 96; best practices 100; SEO 100 except /case (meta description is streamed into the body for non-bot agents by Next 16; bots get it in <head>) and the not-found case (noindex, intended).
- fixed (a11y): /method color-contrast failures (2.0:1 to 4.5:1 on units, captions, table headers): the scroll-driven "rise" faded text from 45% opacity, so anything below the fold was low-contrast until it settled. The rise is now movement only (method.css).
- fixed (perf): the case file's before/after chips (its LCP) were loading="lazy"; now eager with fetchPriority high (EvidenceChips.tsx). Devtools-throttled mobile: /case LCP was 4.2 s with 2.2 s load delay.
- done: keyboard walk of /live (Tab x70): skip link first, rail, sources, 8 layer toggles, map canvas, zoom, attribution, key, motion, replay play/live/slider (labelled), the 10 list rows, more, air rows, investigator examples, question, Ask, then wraps. Enter on a list row opens the case summary, Escape closes it and focus stays on the row.
- fixed (a11y): the investigator's question field showed focus only as a 1px border colour change; now border + 1px outline (2px gold).
- done: failure modes. Server side: a second prod server with API_INTERNAL_URL=http://127.0.0.1:9 (:3200) plus the browser blocking localhost:8000 (full outage): /, /live, /case/18905, /bulletin, /method each paint their shell and say per block what failed ("... did not load. The BlindSpot API is not reachable." with Retry in the app screens); no figure appears, no "undefined/NaN/null". Client side only (browser blocks the API, server fine): server-rendered figures stay, client panels say what failed.
- fixed: /live title row kept a skeleton forever when the orbital elements and the headline both failed; NextLook now says "No pass time right now: the satellites' orbital elements did not load."
- fixed: hero frames that failed showed the browser's broken-image alt text over the hero; Crossfade now hides a failed frame (including one that failed before hydration) and says "This Sentinel-2 picture did not load from the API."
- fixed: case chips that failed before hydration (now that they load eagerly) showed a broken image; on-mount check added, and the scar outline is no longer drawn across the failure note.
- done: phone 390 journey on prod: landing (rail + Menu, pass countdown, CTA), /live (bottom nav, sheet, case summary), /case/18905 -> "Ask the investigator about this case" -> /live?ask=... runs the question (the ?ask= deep link works now; pages.md's open note is closed), /method and /bulletin?lang=hi: no horizontal overflow anywhere.
- Gemini: 1 new live question used (the case page's deep link for case 18905; 7 sources, verified; it is now a stored example). All other investigator checks replayed stored runs.
- done: final rebuild, tsc 0, eslint 0, prod servers on :3100/:3200 stopped; the shared dev server on :3000 left running (serves /, /live, /icon.svg, /opengraph-image.jpg, /robots.txt, 404 on /nope).

## Final numbers (Lighthouse 12.8.2, prod build, local API)

| Route | Form | Perf | A11y | BP | SEO | LCP | TBT | CLS |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| / | mobile | 95 (was 73) | 100 | 100 | 100 | 2.4 s (was 7.0) | 200 ms | 0 |
| / | desktop | 99 | 100 | 100 | 100 | 0.9 s | 20 ms | 0 |
| /live | mobile | 35 (was 33) | 100 | 100 | 100 | 6.4 s (was 9.6) | 8.2 s | 0.149 |
| /live | desktop | 68 | 100 | 100 | 100 | 0.6 s | 2.7 s | 0.05 |
| /live | mobile, devtools throttling | 61 | | | | 1.6 s | 5.6 s | 0.047 |
| /case/18905 | mobile / desktop | 74 / 99 | 100 | 100 | 92 | 5.1 / 0.9 s | 330 / 0 ms | 0.005 / 0 |
| /bulletin | mobile / desktop | 83 / 100 | 100 | 100 | 100 | 4.7 / 0.7 s | 70 / 0 ms | 0.006 / 0 |
| /method | mobile / desktop | 75 / 95 | 100 (was 96) | 100 | 100 | 7.8 / 1.6 s | 60 / 0 ms | 0 |

JS per route (transfer): / 14 files 218 KB (no maplibre, no deck.gl); /live 27 files 818 KB (maplibre-gl 1.1 MB and deck.gl 0.7 MB raw, loaded on idle after hydration); /case 242 KB; /bulletin, /method 233 KB.

## Left open
- /live TBT: evaluating MapLibre 6 + deck.gl costs 2-8 s of main thread on a throttled phone; it now starts after the screen has painted and answers. Shrinking it needs fewer deck.gl layer packages or a lighter map stack.
- /live mobile CLS 0.149 in Lighthouse's unthrottled trace (0.047 with devtools throttling): the Work Sans swap reflows the finding paragraph above the map (0.067) and MapLibre's compact attribution control resizes as it mounts (0.047).
- Lighthouse simulated LCP on mobile for /method (7.8 s) and /case (5.1 s) is dominated by the API's 1600x1000 Sentinel-2 renders (~1.9 MB on /method) competing with the fonts; the API stores featured frames at one size only.
- `next build` prints 1-4 "Unexpected cache miss after cache warming phase" lines for "/" or "/method", depending on the order workers prerender. Traced: short-lived serverGet entries already in the in-memory cache handler are resolved "prerender-dynamic" in the warm phase and not written to the resume cache, so the final phase misses and makes them dynamic holes, the intended result. Benign; silencing it would mean `await connection()` in the shared serverGet.
- `next build` warns metadataBase is unset: on Vercel Next falls back to the deployment URL by itself; elsewhere set NEXT_PUBLIC_SITE_URL before the build.
- Deploy: the API's CORS must allow the web origin (locally only http://localhost:3000 is allowed; the deployed API uses CORS_ORIGIN_REGEX for *.vercel.app).
- API data: the source `s2_cog` has its key as its label ("s2_cog"), which shows in investigator source lists; give it a label in the API's source registry.
- MapLibre console warnings for the OpenFreeMap style's missing sprite images ("circle-11", "wood-pattern"); nothing visible.
- /case meta description is streamed into <body> for non-bot user agents (Next 16 streaming metadata); bots receive it in <head>.
- When the browser cannot reach the API but the server can (rare: same API), /method's server-rendered Sentinel-2 frames show the browser's broken-image box; the landing hero and case chips handle it.
- The Next starter favicon.ico was removed (backup in the QA scratchpad); the OG card generator (opengraph-image.tsx + Anton OFL font) is kept in the scratchpad, the committed card is the static opengraph-image.jpg (128 KB).
- status: QA COMPLETE.
