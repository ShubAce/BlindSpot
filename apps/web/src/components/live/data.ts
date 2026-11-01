/**
 * Mission control's reads: one hook per block, each on the shared SWR client (lib/live.ts), so two
 * panels that need the same endpoint share one request. Heavy layers pass a null path while they
 * are switched off, so nothing large loads until it is asked for.
 *
 * Every figure the screen shows comes from these responses; the helpers below only format and
 * combine what the API sent (sums of returned rows, the newest of two times), never invent a value.
 */

import { useMemo } from 'react';
import { preload, useSWRConfig } from 'swr';

import { getJSON, PATHS, type AirLatestResponse, type FiresResponse, type GeostationaryDiurnalResponse, type Query, type SourcesResponse } from '@/lib/api';
import { MINUS, number, percent } from '@/lib/format';
import { CADENCE, swrKey, useApi } from '@/lib/live';

import type {
  AirLatestLive,
  DiurnalLive,
  FiresDailyLive,
  LiveHeadline,
  LiveScarCase,
  LiveScarsResponse,
  ScarsSummaryLive,
  SmokeBackLive,
  SmokeReceptorsLive,
} from './types';

const SLOW = 30_000;

/** Scars on the map: the newest first (the API's own order), weak candidates dropped. */
export const SCAR_QUERY = { limit: 2500, min_confidence: 0.5 } as const;

/** The IST day of the season replay ("2026-10-24"), or null for live. */
export type ReplayDay = string | null;

/** UTC instant that ends an IST day: the next IST midnight. */
export function istDayEndUtc(day: string): string {
  return new Date(Date.parse(`${day}T00:00:00+05:30`) + 86_400_000).toISOString();
}

export const useLiveHeadline = (fallbackData?: LiveHeadline) =>
  useApi<LiveHeadline>(PATHS.headline, undefined, { refreshInterval: CADENCE.headline, fallbackData });

export const useScarsSummaryLive = () => useApi<ScarsSummaryLive>(PATHS.scarsSummary, undefined, { refreshInterval: CADENCE.scars });

/** The two reads the season replay moves, and their query for a day (null: live). */
type ReplayRead = 'fires' | 'scars';
const replayQuery = (read: ReplayRead, day: ReplayDay): Query =>
  read === 'fires' ? (day ? { hours: 24, end: istDayEndUtc(day) } : { hours: 24 }) : { ...SCAR_QUERY, until: day ?? undefined };
const replayKey = (read: ReplayRead, day: ReplayDay) => swrKey(read === 'fires' ? PATHS.fires : PATHS.scars, replayQuery(read, day));

/** The scars the map draws and the list ranks; `until` an IST day while replaying. */
export const useLiveScars = (day: ReplayDay) =>
  useApi<LiveScarsResponse>(PATHS.scars, replayQuery('scars', day), { refreshInterval: day ? 0 : CADENCE.scars, timeoutMs: SLOW });

/**
 * One district's newest scars, loaded when its tile on the map is clicked, so zooming into a
 * district the newest-overall list misses (Amritsar, Tarn Taran) still finds its fields. One URL
 * per district: the API stores the answer and the CDN caches it like the main list. Live only.
 */
export const useDistrictScars = (district: string | null, day: ReplayDay) =>
  useApi<LiveScarsResponse>(district && !day ? PATHS.scars : null, districtQuery(district), { refreshInterval: CADENCE.scars, timeoutMs: SLOW });
const districtQuery = (district: string | null): Query => ({ ...SCAR_QUERY, district: district ?? undefined });

/** Fire detections: the last 24 h, or the IST day being replayed. */
export const useLiveFires = (day: ReplayDay) =>
  useApi<FiresResponse>(PATHS.fires, replayQuery('fires', day), { refreshInterval: day ? 0 : CADENCE.fires, timeoutMs: SLOW });

/**
 * True while `data` is still the last replay day's answer. SWR keeps it on screen while the day
 * asked for loads (keepPreviousData): the map may keep it, marked as such (its scars faded), so it
 * never blinks empty between days, but a sentence that names the day must not print another day's
 * figures under it, so it says the day is loading instead. `data` is what useLiveFires or
 * useLiveScars returned for `day`.
 */
export function useHeldOver(read: ReplayRead, day: ReplayDay, data: unknown): boolean {
  return useHeldOverKey(replayKey(read, day), data);
}

/** The same for any read: `key` is its SWR key (swrKey(path, query)), `data` what its hook returned. */
export function useHeldOverKey(key: string | null, data: unknown): boolean {
  const { cache } = useSWRConfig();
  return key != null && data !== undefined && cache.get(key)?.data !== data;
}

/**
 * What one replay read may say about `day`, the same for the map, the list and Play: `own` is its
 * answer only when it is this day's (never the last day's held over), `failed` that the read failed
 * with no answer of this day to show, and `ready` that the day is settled either way. `r` is what
 * useLiveFires or useLiveScars returned for the same `day`.
 */
export function useDayRead<T>(read: ReplayRead, day: ReplayDay, r: { data?: T; error?: unknown }) {
  const held = useHeldOver(read, day, r.data);
  const own = held ? undefined : r.data;
  return { held, own, failed: r.error !== undefined && own === undefined, ready: own !== undefined || r.error !== undefined };
}

/** The SWR key of one district's scars (useDistrictScars), for useHeldOverKey. */
export const districtScarsKey = (district: string | null) => (district ? swrKey(PATHS.scars, districtQuery(district)) : null);

/**
 * Start a replay day's fires and scars before Play reaches it, so the step shows its own day at once:
 * SWR hands the waiting request to the hooks when they ask for that day. Days already read are skipped.
 */
export function preloadReplayDay(day: string, cache: ReturnType<typeof useSWRConfig>['cache']): void {
  for (const read of ['fires', 'scars'] as const) {
    const key = replayKey(read, day);
    if (cache.get(key)?.data !== undefined) continue;
    const req: Promise<unknown> | undefined = preload(key, (url: string) => getJSON<unknown>(url, { timeoutMs: SLOW }));
    // a failure is SWR's to report once a hook asks for this day; a day never reached stays quiet
    req?.catch(() => undefined);
  }
}

export const useFiresDailyLive = () => useApi<FiresDailyLive>(PATHS.firesDaily, undefined, { refreshInterval: CADENCE.fires });
export const useDiurnalLive = () => useApi<DiurnalLive & GeostationaryDiurnalResponse>(PATHS.geostationaryDiurnal, undefined, { refreshInterval: CADENCE.geostationary });
export const useAirLive = () => useApi<AirLatestLive & AirLatestResponse>(PATHS.airLatest, undefined, { refreshInterval: CADENCE.air });
export const useSmokeReceptors = () => useApi<SmokeReceptorsLive>('/api/smoke/receptors', undefined, { refreshInterval: CADENCE.smoke });
export const useSmokeBackLive = (city: string | null) =>
  useApi<SmokeBackLive>(city ? PATHS.smokeBack : null, city ? { city } : undefined, { refreshInterval: CADENCE.smoke, timeoutMs: SLOW });
export const useSourcesLive = () => useApi<SourcesResponse>(PATHS.sources, undefined, { refreshInterval: CADENCE.sources });
export const useScarCaseLive = (id: number | string | null) =>
  useApi<LiveScarCase>(id == null ? null : PATHS.scarCase(id), undefined, { refreshInterval: CADENCE.scars });

/**
 * The forward smoke run is 2.5 MB gzip: requested only while the smoke layer is on, thinned with
 * `max_trails` and `stride` (an API that ignores them still sends the full run, which also works).
 */
export const SMOKE_FORWARD_QUERY = { max_trails: 600, stride: 2 } as const;

/**
 * How often the job behind a source runs, from /api/sources (`cadence_minutes`), in ms. A freshness
 * label calls data stale after three of these (the backend's own rule); the polling interval is not
 * the job's cadence, so it is not used for that. Undefined until the sources are read, or for a
 * source with no schedule: then nothing is called stale.
 */
export function useSourceCadence(): (key: string) => number | undefined {
  const src = useSourcesLive();
  return useMemo(() => {
    const by = new Map((src.data?.sources ?? []).map((s) => [s.key, s.cadence_minutes]));
    return (key: string) => {
      const m = by.get(key);
      return m ? m * 60_000 : undefined;
    };
  }, [src.data]);
}

// ---------------------------------------------------------------------------------------------
// wording shared by every block that states a count, area or share of candidate unseen burns

type Precision = { precision?: number | null; precision_ci95?: [number, number] | null; precision_n?: number | null } | null | undefined;

/** "audited precision 87% (95% CI 68–96%, n = 23)", read from the headline; never typed in. */
export function precisionText(u: Precision): string | null {
  if (!u || u.precision == null) return null;
  const ci = u.precision_ci95 && u.precision_ci95.length === 2 ? `95% CI ${percent(u.precision_ci95[0])}–${percent(u.precision_ci95[1])}` : null;
  const n = u.precision_n != null ? `n = ${number(u.precision_n)}` : null;
  const inner = [ci, n].filter(Boolean).join(', ');
  return `audited precision ${percent(u.precision)}${inner ? ` (${inner})` : ''}`;
}

/** A signed or plain change in the true minus. */
export const minus = (s: string) => s.replace(/^-/, MINUS);

/** "Amritsar, Ludhiana and Patiala" */
export function listJoin(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? '';
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** "11:30" (IST "HH:MM") minutes since midnight. */
export function hhmmMinutes(s: string | null | undefined): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(s ?? '');
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}
