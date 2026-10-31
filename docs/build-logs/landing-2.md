# landing round 3 build log ('/', area design-landing)

## 2026-10-31
- done: read architecture, contract notes, landing.md (round 2 = 8.1), taste notes/copy/direction, current landing code.
- next: check live data shapes (orbits/passes, geostationary/diurnal, scars?bbox, headline) on :8000; screenshot the
  current page; then build (1) blind-spot clock, (2) scar outlines on hero frames, (3) key figure, (4) sparse sections.
- done: before-inspect (.taste-skill/landing3-before: 1 FAIL eyebrow@768 from the cut proof card, 7 WARN); live shapes
  checked: passes (VIIRS/MODIS 3-7 min each; today 12 looks), diurnal hourly_ist (peak 16-17), scars?bbox (115 in the
  featured frame: 2/4/26/115 by frame, 21 FIRMS-confirmed), risk.method.note, sources kinds.
- plan: hero = header bar (h1 + supporting sentence + nav, not sticky) then a grid: picture 1.6:1 uncropped with
  scar outlines (left, cols 1-7) | blind-spot clock (right) ; key figure under the picture | legend + live line under
  the clock. Phone: header, figure, clock, legend, picture. Clock = clock.ts (pure model: today's IST passes for
  REFERENCE_SITE, circular gaps, GK-2A hourly bars) + BlindClock.tsx (client; SSR fallback; now = passes.generated_at
  until the browser clock runs). Live line from the same passes (drops satellite.js from '/').
  Showcase drops its duplicate 83% claim. Intro: big photo + thesis numbers. Harvested: no score, wider table, caveat
  quote from method.note. Proof: sources grouped by kind, labels without parentheticals.
- next: write clock.ts, BlindClock.tsx, Hero.tsx, overlay in Crossfade.
- done: clock.ts + BlindClock.tsx + Hero.tsx (clock, key figure), HeroPictures/Crossfade (outlines per frame + counts),
  Rail.tsx (static header: h1, thesis sentence, nav), page.tsx (hero inside main), Intro (statement from GK-2A +
  FIRMS count, large photo, today's pass timetable), Showcase (scope line instead of the duplicate 83%), Harvested
  (no score; table district/village/ha/fields; method.note quoted; backtest), Proof (sources grouped by kind, short
  labels), CSS. Deleted IntroLive.tsx, PassLine.tsx, RailSentence.tsx (no satellite.js on '/' now). tsc 0, eslint 0.
- next: screenshots of every section at 3 widths, fix, then inspect.mjs + lint.mjs.
- done: OneDay evening moment now says "no fire detection reached FIRMS from X to Y" (FIRMS gap) so it does not read
  as a second, different satellite gap next to the clock's; failure simulation (bad diurnal + scars paths, reverted):
  clock keeps its gold marks and says GK-2A did not load; picture says the scars did not load. Reduced motion: no
  hand sweep, no outline fade. Stale style keys pruned.
- checks: tsc 0, eslint 0, taste lint 0/0, inspect (.taste-skill/landing3-final) 0 FAIL / 3 WARN (no footage; drag
  row by design; One Day head row) vs before 1 FAIL / 7 WARN. Phone (dev): LCP element = hero audit text, CLS 0, no
  maplibre/deck.gl/satellite.js requests on '/'.
- critique round 3: seen in one second = a 24-hour dial with a bite "NOBODY LOOKING 5 H 01 MIN" beside Punjab fields
  whose burn scars light up gold pass by pass, and "83%" in large type. Scores: design 8.5 · usability 8.5 ·
  creativity 9 · content 9 -> about 8.7. Remaining: the stored investigator answer quotes the FIRMS gap (16:34-01:06),
  a different measure from the clock's pass gap; no footage; Lighthouse not run (no prod build allowed here).
- status: COMPLETE.

## Repair (review findings, 2026-10-31)
- done: read findings; API checked on :8000: diurnal polar_windows_ist (6 windows, 3 days, Punjab+Haryana, from FIRMS
  detections), unobserved_gaps_ist with pixel_hours (16:30-20:41 = 476.0 of 708.7 px-h), headline
  gk2a_evening.unobserved_span_ist = the same gap (max pixel_hours). Stored examples: local has a case-18905 answer;
  prod has 3 (the 'missed' one carries the 16:34-01:06 FIRMS gap point).
- plan: (1) clock gold = diurnal polar windows, bite = the gap with most GK-2A activity (= headline span), hub
  "Fire satellites away", legend 76% now against the drawn gold; fallback to today's Sangrur passes if diurnal fails;
  live line names the fire satellites over Sangrur. (2) One Day evening uses the same span; investigator slip skips
  points with an unobserved span or a hectare total, prefers a case answer. (3) Crossfade opens on the newest frame.
  (4) caption: "burn scars", natural wording. (5) hand shorter. (6) slip focus ring gold. (7) slips not cut on phone.
  (8) Intro timetable caption no longer says "the clock's gold marks".
- next: clock.ts + BlindClock.tsx + Hero.tsx.
- done: clock.ts (region scope from diurnal polar windows + API blind span; site fallback), BlindClock (hub "No fire
  satellite", live line over Sangrur, legend 76% outside gold / 67% inside bite, hand 124-212, hour labels out),
  Hero (draws without passes if diurnal ok; "burn scars"), Crossfade (opens on newest frame; tallyLine caption),
  OneDay (headline span), Intro caption, Showcase (case answer first; avoid span/ha points; chip under head), CSS
  (gold slip focus ring). tsc 0, eslint 0. Probes: hand/hub clearance >= 16 px by bbox at 06:01/18:02/07:40/16:20;
  fallback simulated (bad diurnal path, reverted, diffed): site passes + longest gap, says what failed.
- next: early-frame caption check, inspect.mjs + lint.mjs, final checks.
- done: captions per frame read: 13 Oct "2 burn scars ... None had a fire alert: both are candidate unseen burns";
  28 Oct "115 burn scars (913 ha). 21 had a fire alert (white); 94 had none". Slip chips under the head (16/9),
  shortest slips 34/35svh so content fits above the pocket edge (an interim run flagged a hidden "Confidence" line
  as an eyebrow); focus ring gold (probe: rgb(201,164,90)). Server HTML: newest frame visible + fetchPriority high,
  desc names 16:30-20:41 and 67%; no "nobody" on the page.
- checks: tsc 0, eslint 0, taste lint 0/0, inspect (.taste-skill/landing3-repair) 0 FAIL / 3 WARN (same three as
  before: no footage, One Day drag row, One Day head row). No pytest (no backend file touched).
- status: REPAIR COMPLETE.
