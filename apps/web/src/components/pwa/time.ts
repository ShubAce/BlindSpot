import { useSyncExternalStore } from 'react';

import { dateTimeIST, relative, timeIST } from '@/lib/format';

// Both offline views say when a copy was saved: "22:41 IST (14 min ago)". lib/live's useNow ticks for
// the data panels, but importing it here would put SWR and the API client into the root layout of
// every page; this is the one clock these two views need.
const STEP = 15_000;
const tick = (cb: () => void) => {
  const t = window.setInterval(cb, STEP);
  return () => window.clearInterval(t);
};
const idle = () => () => undefined;

/** Now in 15 s steps, rounded up, so a copy saved a moment ago never reads as saved in the future.
 *  0 on the server, during hydration, and while `on` is false: no timer runs while no time shows. */
export function useClock(on = true): number {
  return useSyncExternalStore(
    on ? tick : idle,
    () => (on ? Math.ceil(Date.now() / STEP) * STEP : 0),
    () => 0,
  );
}

/** "22:41 IST" for a copy from the last 20 hours, "30 Oct, 22:41 IST" for an older one. */
export const savedStamp = (at: number, now: number): string =>
  now - at < 20 * 3_600_000 ? timeIST(at, { suffix: true }) : dateTimeIST(at, { suffix: true });

/** "14 min ago" or "just now", never "in 3 s". */
export const savedAgo = (at: number, now: number): string => relative(at, new Date(Math.max(now, at)));
