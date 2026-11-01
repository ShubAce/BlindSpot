import { useMemo } from 'react';

import { inShape } from '@/components/map/geometry';
import { centroidOf, PATHS, type ApiError, type LonLat } from '@/lib/api';
import { istDayKey } from '@/lib/format';
import { useApi } from '@/lib/live';

import type { LiveScarsResponse } from './types';

/**
 * A case number the API no longer has, followed to its field's current one. The daily Sentinel-2 run
 * re-measures the newest passes and stores their fields afresh under new numbers (services/api
 * jobs/sentinel.py, store_tile), so a scar list made before the run can name numbers that are gone
 * (404 "no such scar"): clicked, such a field would open nothing. It is the same field when a scar of
 * the same pass now holds its centre.
 */

type LiveScar = LiveScarsResponse['features']['features'][number];

/** Half the side of the box the field is looked for in, in degrees (about 110 m north to south): a
 *  scar whose outline holds the old centre always overlaps it. */
const BOX_DEG = 0.001;

const box = ([lon, lat]: LonLat) => [lon - BOX_DEG, lat - BOX_DEG, lon + BOX_DEG, lat + BOX_DEG];

/**
 * The scar among `candidates` that is `gone`'s field re-measured: another number, the same pass (the
 * day of the burnt pass: Sentinel-2 passes over a field at most once a day, and the run replaces a
 * pass's fields by that day), an outline that holds the old centre. Where two do (two of the run's
 * stages drew the field), the one at the same resolution, then the nearest in area. Null when none
 * does: no other scar is guessed at.
 */
export function successorOf(gone: LiveScar, candidates: readonly LiveScar[]): LiveScar | null {
  const g = gone.properties;
  const at = centroidOf(g.centroid);
  const day = istDayKey(g.burn_before);
  if (!at || !day) return null;
  const held = candidates.filter((f) => f.properties.id !== g.id && istDayKey(f.properties.burn_before) === day && inShape(f.geometry, at[0], at[1]));
  const sameRes = held.filter((f) => f.properties.res_m === g.res_m);
  const gap = (f: LiveScar) => Math.abs(f.properties.area_ha - g.area_ha);
  return (sameRes.length ? sameRes : held).reduce<LiveScar | null>((best, f) => (best && gap(best) <= gap(f) ? best : f), null);
}

export type Successor = {
  /** The field's current scar: null until the box is read, and when it holds none. */
  next: LiveScar | null;
  /** Whether the box around the field has been read. */
  looked: boolean;
  error: ApiError | undefined;
  retry: () => void;
};

/**
 * The scars in a small box around `gone`'s centre, and its field among them (successorOf). Nothing is
 * read while `gone` is null. `gone` in the URL is not an API parameter (the API ignores it): it gives
 * each lookup its own address, so the CDN never answers it with a copy made before the number was
 * gone, such as one from the same field's lookup on an earlier day.
 */
export function useScarSuccessor(gone: LiveScar | null): Successor {
  const at = gone ? centroidOf(gone.properties.centroid) : null;
  const r = useApi<LiveScarsResponse>(at ? PATHS.scars : null, at && gone ? { bbox: box(at), limit: 50, gone: gone.properties.id } : undefined, {
    refreshInterval: 0,
    revalidateOnFocus: false,
    keepPreviousData: false,
  });
  const next = useMemo(() => (gone && r.data ? successorOf(gone, r.data.features.features) : null), [gone, r.data]);
  return { next, looked: r.data !== undefined, error: r.error, retry: () => void r.mutate() };
}
