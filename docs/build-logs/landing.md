# landing build log ('/')

## 2026-10-31
- done: read contract notes, copy deck, notes, direction sheet, foundation code, design-taste skill; checked live data
  (headline, featured, scars unseen, case, risk ranking, agent examples, sources) and NASA GIBS (describeDomains gives
  the latest day per layer; WMS epsg3857 works keyless; NOAA-20 DayNightBand gives a real night picture of Punjab).
- decisions: ISRO "peak near 5 PM" is not in the API or architecture.md, so the intro and the "five in the evening"
  moment use the live GK-2A block of /api/stats/headline (peak_ist, share_outside_polar_windows) instead of typing it.
  Proof card names come from /api/sources labels (live), not a typed list. Picture of "The night drift" = GIBS MODIS
  wide view (no smoke image endpoint exists). Added CC photo "National Agro Happy Seeder" (FatherofGod, CC BY-SA 4.0).
- next: photo-credits.ts, landing data loaders, hero + rail, sections 1-7, then tsc/lint/inspect/screens.
- done: built page.tsx + src/components/landing/* (Rail, Crossfade, HeroPictures, Intro/IntroLive, Showcase, Harvested, OneDay/DragRow,
  Proof/SourcesLine, Reply/DelhiAir, Footer, Pic, Reveal, gibs.ts, data.ts, types.ts, words.ts) and src/lib/photo-credits.ts;
  deleted public/photos/credits.json.
- found: CSS Modules are OFF under the shared turbopack "*.css" rule in next.config.ts (module classes come out undefined),
  so the landing uses a global stylesheet landing.css with lp- prefixed classes mapped in styles.ts. Tell other agents.
- done: inspect.mjs 0 FAIL at 1440/768/390 (5 WARN: no footage - no honest Punjab clip exists in the open collections;
  side-scroll = the drag row by design; pinned/long-pin = the rail docks as the header bar by design; empty-middle under review).
- next: critique round on screenshots, view transition to /live, a11y pass, final tsc/lint.
- done: interaction check (scratch CDP script): no console errors; crossfade advances; date buttons hold a pass; drag row
  buttons scroll by one column and move the bar; rail docks after the pictures; phone Menu opens; reduced motion holds the
  newest pass; CTA navigates to /live with the 'to-live' view transition (page steps back and fades).
- done: fixed entrances flattening tilts (cards/slips now use the `rotate` property, not transform); list photo larger.
- done: tsc 0 errors, pnpm lint clean, taste lint 0/0, inspect.mjs 0 FAIL at 1440/768/390.
- status: COMPLETE. WARNs defended: no-footage (no honest Punjab clip in open collections), side-scroll (the drag row),
  pinned/long-pin (the rail is the docked header bar by the hero blueprint), empty-middle (persists after the day-row
  aside line was added; likely measured on the row of moments - check visually, it reads fine).

## Critique (juror pass, 2026-10-31)
- done: inspect.mjs r0 (0 FAIL, 5 WARN), own viewport screenshots at 1440 and 390, network (no maplibre/deck.gl,
  no failed requests, CLS 0), console (0 errors; one Next LCP hint).
- ROUND 1 findings: (1) typed times on every screen: rail/phone sentence "Satellites look at 1:30. Fires start at 5."
  and the PassLine/IntroLive fallbacks "near 1:30 PM and 1:30 AM" are not API values (zero-tolerance), and "start at 5"
  contradicts the page's own GK-2A peak 16:00-17:00; (2) the investigator slip printed raw markdown "- " bullets and its
  typed divider ran on the question line ("Punjab?- - -"); the chosen answer ("satisfactory range") undersold the USP;
  (3) the reply paragraph ran 13 lines at statement size (blueprint: a short paragraph).
- done: RailSentence.tsx (server, in Suspense): "No fire satellite looks from {unobserved_span_ist.start} to {end} IST."
  from /api/fires/hours, else GK-2A peak; number-free words while loading/failing. Rail/HeroFoot take it as a prop.
  PassLine/IntroLive fallbacks now say where times come from, no typed time.
- done: answer slip: lead + 2 points as a real list (answerParts in words.ts), prefers the "missed/unseen" example.
- done: DelhiAir shortened; smoke trace called a model; the "ask" sentence moved into the reply card.
- next: re-screenshot, failure-state simulation, keyboard/focus pass, tablet.
- done: failure simulation (temporary bad paths in data.ts + Proof.tsx, reverted and verified): every block says what
  failed in one line; client polls recover the intro, Delhi air and source line. Fixed: the intro kept the server's
  "did not load" line under recovered numbers (now shown only while the browser has no data either); the footer stamp
  showed an empty white square (now absent); rail fallback shortened to one line, no figure.
- done: keyboard pass (Tab x60): skip link first, every control reachable with a 2px visible ring, nothing focused
  under the docked rail; added scroll-padding-top under the sticky rail for #main/#sources/#credits.
- done: tablet: answer slip moved under the claim (cols 4-12) instead of a 5-col sliver; phone shows the lead + one point.
- done: answer slip now shows one point (unobserved hours); a scar total scoped to the investigator's question beside
  the season total read as a contradiction.
- checks: tsc 0, eslint clean, taste lint 0/0, inspect r2 0 FAIL (6 WARN, defended: no honest footage; drag row by
  design; rail docks by blueprint (pinned/long-pin); One Day head row; lopsided@768 is the full-bleed GIBS figure whose
  caption sits right), CLS 0, 0 console errors, no maplibre/deck.gl requests on '/'.

ROUND 2
Seen in one second: the Punjab fields from Sentinel-2 crossed by a dark rail.
Idea: the satellites' blind hours become the rail that crosses every picture (live gap + live pass clock).
Live thing: the pass clock and countdown in the rail, computed from orbital elements in the browser.
Layout words: hero=full-bleed / intro=split / showcase=full-bleed+slips / list=table / how=drag row / proof=card /
  cta=card / footer=postcard.
Scores: design 8 · usability 8 · creativity 8 · content 8.5 -> overall 8.1
Caps applied: none (0 FAIL; no eyebrows, gradients, rules beyond the blueprint's typed slip divider).
Remaining: first frame is the oldest pass (scars appear only on frames 3-4, about 14 s in); proof card is long on a
  phone; no footage.
- status: critique COMPLETE.
