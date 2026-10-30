/**
 * Orbit hooks for client components: the satellites from /api/orbits/tle and "who is watching now",
 * recomputed every second from the elements in the browser (no request per tick).
 * Kept apart from lib/live.ts so pages that never draw a satellite do not load SGP4.
 *
 *   const { status, now } = useLiveStatus(REFERENCE_SITE);
 *   <p>{status && now ? liveSentence(status, now, 'Punjab') : <Skeleton width="44ch" height={22} />}</p>
 */

import { useMemo } from 'react';

import { REFERENCE_SITE, type Site } from './aoi';
import { duration, timeIST } from './format';
import { useNow, useOrbitsTle } from './live';
import { FIRE_INSTRUMENTS, liveStatus, parseTles, type LiveStatus, type Satellite } from './orbits';

/** The followed satellites, parsed once per element set. */
export function useSatellites() {
  const tle = useOrbitsTle();
  const sats: Satellite[] = useMemo(() => parseTles(tle.data), [tle.data]);
  return { sats, tle };
}

/** Who is watching `site` now and who looks next, updated every `periodMs`. Null until both the
 *  elements and the browser clock are available (never during server rendering). */
export function useLiveStatus(site: Site = REFERENCE_SITE, periodMs = 1_000, instruments: readonly string[] = FIRE_INSTRUMENTS) {
  const tle = useOrbitsTle();
  const now = useNow(periodMs);
  const status: LiveStatus | null = useMemo(
    () => (now && tle.data?.satellites?.length ? liveStatus(site, tle.data, now, instruments) : null),
    [now, tle.data, site, instruments],
  );
  return { status, now, tle, error: tle.error, isLoading: tle.isLoading };
}

/**
 * The landing page's live sentence (.taste-skill/notes.md, "The live thing"):
 *   "No fire satellite can see Punjab right now. NOAA-21 passes at 13:12 IST, in 9 h 41 min."
 *   "NOAA-20 is over Punjab now. It leaves in 6 min."
 */
export function liveSentence(status: LiveStatus, now: Date, place = 'Punjab'): string {
  const w = status.watching[0];
  if (w) return `${w.name} is over ${place} now. It leaves in ${duration(w.until.getTime() - now.getTime())}.`;
  const n = status.next;
  const head = `No fire satellite can see ${place} right now.`;
  return n ? `${head} ${n.name} passes at ${timeIST(n.at)} IST, in ${duration(n.at.getTime() - now.getTime())}.` : head;
}
