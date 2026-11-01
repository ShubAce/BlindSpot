/**
 * How often each job refreshes its data (architecture.md section 6, research SYNTHESIS.md), in ms.
 * The SWR hooks poll at these intervals (polling faster would only re-read the same rows), and a
 * freshness label calls data stale after three cadences, the backend's own rule for /api/sources.
 */

const MIN = 60_000;

export const CADENCE = {
  health: 1 * MIN,
  sources: 1 * MIN,
  headline: 1 * MIN,
  orbits: 1 * MIN,
  passes: 1 * MIN,
  fires: 5 * MIN,
  air: 5 * MIN,
  geostationary: 5 * MIN,
  met: 10 * MIN,
  wind: 10 * MIN,
  smoke: 10 * MIN,
  scars: 15 * MIN,
  risk: 15 * MIN,
  imagery: 15 * MIN,
  bulletin: 15 * MIN,
  official: 15 * MIN,
  stubble: 30 * MIN,
  /** Boundaries and cities do not change during a session. */
  static: 0,
} as const;

/**
 * When data built by the daily Sentinel-2 run (scars, risk, case files) is late. The polling cadences
 * above are how often the browser re-reads, not how often this data changes, so three of them would
 * call a case built this morning stale. This is the API's own rule for that job: its schedule's longest
 * gap plus an hour (docs/api-contract-notes.md, "Headline freshness").
 */
export const DAILY_RUN_STALE_MS = 26 * 60 * MIN;
