/**
 * "Who is watching Punjab": the site, the satellites and their looks, computed in the browser from
 * the CelesTrak elements the API serves (lib/orbits.ts; matches /api/orbits/passes to under 1 s).
 * The site is the one /api/stats/headline counts passes for (the Punjab state centroid), so the
 * header, the pass strip and the map's pass footprints all describe the same place. If the headline
 * cannot be read, the site falls back to the research test site in lib/aoi.ts and says so by name.
 */

import { useMemo } from 'react';

import { REFERENCE_SITE, type Site } from '@/lib/aoi';
import { useNow } from '@/lib/live';
import { FIRE_INSTRUMENTS, liveStatus, predictPasses, type LiveStatus, type PassWindow } from '@/lib/orbits';
import { useSatellites } from '@/lib/use-orbits';

import { useLiveHeadline } from './data';

export type WatchSite = Site & { name: string; place: string };

export function useWatchSite(): WatchSite | null {
  const h = useLiveHeadline();
  return useMemo(() => {
    const s = h.data?.passes?.site;
    if (s && Number.isFinite(s.lat) && Number.isFinite(s.lon)) return { lat: s.lat, lon: s.lon, name: s.name ?? 'Punjab', place: 'Punjab' };
    if (h.error && !h.data) return { ...REFERENCE_SITE, place: REFERENCE_SITE.name };
    return null;
  }, [h.data, h.error]);
}

/** Live status (who watches now, the next and the last look), recomputed every `periodMs`. */
export function useWatch(periodMs = 1_000) {
  const site = useWatchSite();
  const { sats, tle } = useSatellites();
  const now = useNow(periodMs);
  const status: LiveStatus | null = useMemo(
    () => (site && now && tle.data?.satellites?.length ? liveStatus(site, tle.data, now) : null),
    [site, now, tle.data],
  );
  return { site, sats, tle, now, status };
}

/** Fire-satellite looks at the site between `from` and `from + hours`, recomputed per element set. */
export function usePassWindows(site: WatchSite | null, sats: ReturnType<typeof useSatellites>['sats'], from: number | null, hours: number): PassWindow[] {
  return useMemo(() => (site && from != null && sats.length ? predictPasses(site, sats, new Date(from), hours, FIRE_INSTRUMENTS) : []), [site, sats, from, hours]);
}
