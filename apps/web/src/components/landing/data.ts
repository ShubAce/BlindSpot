import 'server-only';

import { PATHS } from '@/lib/api';
import { REFERENCE_SITE } from '@/lib/aoi';
import { serverGet, type ApiResult } from '@/lib/api-server';

import type { AgentExamples, Diurnal, Featured, FireHours, FrameScars, Headline, MetNow, OrbitPasses, Risk, ScarCase, ScarList } from './types';

/**
 * The landing page's reads, one per endpoint, through serverGet (short cache, never throws). Each
 * section awaits only what it shows, inside its own <Suspense>, so a slow endpoint holds back one
 * block and the rest of the page paints.
 */

export const getHeadline = () => serverGet<Headline>(PATHS.headline);
export const getFeatured = () => serverGet<Featured>(PATHS.imageryFeatured);
export const getRisk = () => serverGet<Risk>(PATHS.risk, { limit: 1 }, 15_000);
export const getExamples = () => serverGet<AgentExamples>(PATHS.agentExamples);
export const getFireHours = () => serverGet<FireHours>(PATHS.firesHours, { days: 7 });
export const getDelhiMet = () => serverGet<MetNow>(PATHS.metNow, { city: 'Delhi' });

/** The fire sensors' passes over the reference site (Sangrur), 30 h either side of now: enough for
 *  the whole IST day on the clock and for the next look. Fixed parameters, so the read caches. */
export const getPasses = () =>
  serverGet<OrbitPasses>(PATHS.orbitsPasses, { lat: REFERENCE_SITE.lat, lon: REFERENCE_SITE.lon, hours_back: 30, hours_ahead: 30, instruments: 'VIIRS,MODIS' });

/** GK-2A's fire activity by IST hour over the last days (the clock's red bars). */
export const getDiurnal = () => serverGet<Diurnal>(PATHS.geostationaryDiurnal);

/** Every burn scar inside a picture's box (the outlines on the hero's frames). */
export const getFrameScars = (bbox: number[]) => serverGet<FrameScars>(PATHS.scars, { bbox: bbox.join(','), limit: 2000 }, 15_000);

/** How many case slips the showcase stands up. */
export const SLIPS = 6;

/**
 * Candidate unseen burns for the slips: the API's most confident unseen scars, one per village so the
 * six slips are six places, largest first. Then each one's case file, for its picture.
 * `failed`: a read behind them failed (the list, or a case file, whose slip then has no picture), so
 * the page carries the failure mark (components/pwa/LoadFailed) and the offline worker keeps its good
 * copy instead of this one.
 */
export async function getCases(): Promise<{ list: ApiResult<ScarList>; cases: ScarCase[]; failed: boolean }> {
  const list = await serverGet<ScarList>(PATHS.scars, { unseen: true, min_confidence: 0.9, limit: 120 }, 15_000);
  if (!list.ok) return { list, cases: [], failed: true };
  const seen = new Set<string>();
  const picked = list.data.features.features
    .map((f) => f.properties)
    // `unseen === true` only: an unchecked scar (null) is never called unseen.
    .filter((p) => p.unseen === true)
    .sort((a, b) => b.area_ha - a.area_ha)
    .filter((p) => {
      const k = `${p.village ?? ''}|${p.district ?? ''}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .slice(0, SLIPS);
  const files = await Promise.all(picked.map((p) => serverGet<ScarCase>(PATHS.scarCase(p.id))));
  const cases = picked.map((p, i) => {
    const r = files[i];
    return r.ok ? r.data : ({ case: p, imagery: null, generated_at: list.data.generated_at, sources: [] } as ScarCase);
  });
  return { list, cases, failed: files.some((r) => !r.ok) };
}
