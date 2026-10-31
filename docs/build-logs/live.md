# live (mission control) build log

Owner: src/app/live/*, src/components/live/* (except src/components/live/investigator/*).

## 2026-10-31
- done: read contract notes, taste notes, app direction (data-terminal, icon rail), app-screens.md, foundation code.
- next: probe live API shapes for /live endpoints; write types (additive) and build the shell.
- done: built src/app/live/page.tsx and src/components/live/* (state, map commands, lazy map canvas, toggles+key, case summary, season replay, ranked list, air at receptors, sources, pass strip panel, icon rail, finding line, next look, investigator slot). tsc clean.
- found+fixed (shared, additive): `.module.css` files compile as plain global CSS under this app's Turbopack rule (styles object is empty) -> /live uses a global stylesheet with `lv-` prefixed classes (components/live/live.css + styles.ts). The landing agent hit the same thing.
- found+fixed (shared, additive): every map was blank: MapLibre 6 could not find its worker ("Worker failed to load") and deck.gl 9.4's interleaved overlay read `map.transform`, which MapLibre 6 moved to the camera. New components/map/compat.ts (ensureMapWorker, patchMapForDeck), called from BaseMap (module scope + handleLoad). DeckOverlay: MapboxOverlay gets deviceProps {_reuseDevices: true} (dev double-mount threw "WebGL context already attached").
- found+fixed (shared, additive): PassStrip got optional geoLabel/geoUnit props; its peak label moves inside the bar when tall (it collided with the row caption).
- next: rebalance the 1440x900 layout (map taller, sources as a compact title-row control), then 768/390, states, keyboard.
- done: 1440x900 rebalanced (sources became a title-row control with a popover; replay is one row under the map; strip header holds its key); inspector --app: 0 FAIL; tablet side cards level (contain: size); phone sheet works; replay, case summary, risk, smoke forward and back, wind checked live in the browser.
- done: staleness now uses each source's own job cadence from /api/sources (`useSourceCadence`), not the poll interval (every panel said "stale" before).

## Investigator contract

The console lives in `src/components/live/investigator/*` (its owner) and is mounted by
`src/components/live/InvestigatorSlot.tsx` (replace its `return null` with the console). Layout:
the slot is the screen grid's `console` area. On screens wider than 1100px it is a third column to
the right of the ranked list, spanning the full height (title row to the strip); its width is the
console's own (400px suggested; the map column shrinks). The column exists only while the slot
renders something (`.lv-screen:has(> .lv-console > *)`), so nothing shows before the console does.
At 1100px and below it is a full-width block after the side panels; on a phone (700px and below)
the bottom 56px is the nav bar and the 52px above it is the ranked-list sheet, so a console drawer
there should sit above `bottom: 108px` or take over the sheet's place. Use the app tokens
(`[data-surface="app"]`, the console is inside it) and the `lv-` classes in live.css where they fit
(`lv-card`, `lv-cardHead`, `lv-cardTitle`, `lv-pill`, `lv-ctrl`).

Everything happens inside `<LiveProvider>`, so the console can call:

```ts
import { useMapCommands, AGENT_LAYERS } from '@/components/live/map-commands';
import { useLiveState, type LiveLayer } from '@/components/live/live-state';

const map = useMapCommands();
map.applyAgentEvent(ev);              // an SSE `map` event as sent: {action: 'show_layer'|'fly_to'|'highlight', payload}
map.flyTo({ lon, lat, zoom? });        // or { bbox: [w, s, e, n], padding? }; jumps under reduced motion; waits for the map if it is still loading
map.highlight('scar' | 'risk_cluster' | 'fire' | 'station', ids, { replace = true, focus = false }); // focus frames them; switches the kind's layer on
map.markPlace({ lon, lat, label }, { replace = true });   // a ring and a label (districts, cities, villages, points)
map.showLayers(['fires', 'smokeBack'], { only?, city? }); // city: whose back trajectories `smokeBack` shows
map.hideLayers(['wind']);
map.openCase(scarId | null);          // the case summary over the map, flies to the field
map.clearHighlights();
map.goLive();                          // leave the season replay
```

`LiveLayer` = fires | scars | risk | smoke (forward trails) | smokeBack (air reaching a city) | wind
| air | satellites. `applyAgentEvent` maps the agent's layer names (services/api agent/tools.py
MAP_LAYERS: fires, scars, unseen_scars, risk, smoke_forward, smoke_back (+params.city),
air_stations, wind, satellite_passes; imagery is not on this screen and returns false) and its
highlight types (scar opens the case summary; risk_cluster, fire, station outline the feature;
district, city and village are marked from `payload.center`/`payload.bbox` with `payload.label`).
It returns false for an event it could not apply. `useLiveState()` exposes the current layers,
replay day, selected scar and highlights if the console wants to show them. Data hooks the
console may reuse without new requests: `useLiveHeadline`, `precisionText` (components/live/data.ts).
- done: pause/play for moving layers (MotionToggle, WCAG 2.2.2), error states checked by blocking endpoints in the browser, reduced motion checked (wind and smoke draw still), phone type floor 13px, floating shadows marked. Inspector --app: 0 FAIL, 1 WARN (edge-text on the phone: the layer toggles scroll sideways inside their row; defended). Taste lint: 0 FAIL.
- status: COMPLETE for this part. Open: the investigator console goes into InvestigatorSlot (see contract above).

## Critique

Fresh-eyes review of /live (mission control and the investigator console), 2026-10-31. Screens read
at 1440x900, 1440x1000, 1920x1080, 768x1024 and 390x844 (fold, full page, case open, example
replayed, endpoints blocked, keyboard tab order). Screenshots: .taste-skill/critique-live/{before,after}
and the scratchpad's crit/ folder.

ROUND 1 (before)
Seen in one second: the finding figure "86,303 ha", then a small, crowded map.
Idea (one sentence): the satellites' blind hours become the screen's clock (pass strip, next look, replay).
Live thing: the investigator driving the map; the season replay; the live countdown.
Layout words: title=finding line / map=map with key / side=ranked list + air table / strip=timeline / console=column.
Scores: design 6.5 · usability 6.5 · creativity 8 · content 7 → overall 6.9.
Top three problems:
1. 1440x900: the map body was ~275px tall and ~600px wide; the open key covered half of it and the
   case summary covered the rest, so the field a judge clicks on was hidden under its own card.
2. Data truth: the ranked list said "3,552 ha in 2,257 fields with no fire alert, burnt 25-30 Oct" from
   a truncated page (/api/scars returned 2,500 of 48,104 with truncated=true), read as a complete count.
3. Pass strip: the gap length "4 h 40 min" sat under the Now line and its label; the strip key wrapped
   right-aligned with an orphan item.
Also: typed fallbacks ('925 hPa' when the wind level is missing, "about 800 m up", "last 72 h",
"confidence 0.5"); MapLibre's canvas and zoom buttons had no visible focus ring.

- done: the screen grows to max(100svh, 1000px) on short screens so the map keeps ~400px of body; on
  wide screens under 1000px tall the console spans the column from the top and is sticky, so its
  question box is always on screen (live.css, end of file).
- done: the key opens by default only on wide screens at least 1000px tall, and folds while a case
  summary is open (MapPanel). The case flight frames the field in the part of the map the card leaves
  clear (LiveMapCanvas `clearOf`).
- done: ranked list states truncation from the response: "The newest 2,500 of 48,104 mapped scars hold
  2,257 with no fire alert, 3,552 ha, burnt 25-30 Oct", sheet handle "2,257 newest" (UnseenList).
- done: key wording reads the wind level (`level`) and back-run span (`hours`) from the API, the scar
  confidence floor from SCAR_QUERY; no typed fallback (legend.ts `LayerKeyMeta`, `levelText`).
- done (shared, small fix): PassStrip moves a gap's length beside the Now line, or drops it when no
  side fits. Strip key goes under the title, left-aligned, when the strip is under 940px (container query).
- done: visible focus on the MapLibre canvas and controls inside /live.
- checked: no maplibre/deck chunk on the landing page; on /live they load after first paint (lazy
  chunk, ~330 ms in dev); FCP ~0.8 s on the dev server. Error states with scars, air, receptors,
  TLE, GK-2A and daily fires blocked: each block says what failed with Retry, the rest renders.
  Tab order: skip link, rail, sources, layer toggles, map, zoom, key, motion, replay, list rows, air,
  console. tsc 0, eslint 0, inspect --app 0 FAIL 1 WARN (phone toggle row scrolls inside its own row,
  defended), taste lint 0 FAIL.

ROUND 2 (after)
Seen in one second: the finding figure, then the map with the burn belt lit.
Scores: design 7.5 · usability 7.5 · creativity 8 · content 8 → overall 7.7.
Still short: at 768 the ranked list shows four rows beside the air card; the GK-2A row caption is
crossed by a grid line (shared PassStrip); the phone layer toggles scroll sideways; the investigator's
step summaries print the API's raw figures (67854, 104476.3) as the tool returned them.
- status: critique round complete.
