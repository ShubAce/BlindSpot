import 'server-only';

import { PATHS } from '@/lib/api';
import { serverGet, type ApiResult } from '@/lib/api-server';

import type { AgentExamples, Featured, FireHours, Headline, MetNow, Risk, ScarCase, ScarList } from './types';

/**
 * The landing page's reads, one per endpoint, through serverGet (short cache, never throws). Each
 * section awaits only what it shows, inside its own <Suspense>, so a slow endpoint holds back one
 * block and the rest of the page paints.
 */

export const getHeadline = () => serverGet<Headline>(PATHS.headline);
export const getFeatured = () => serverGet<Featured>(PATHS.imageryFeatured);
export const getRisk = () => serverGet<Risk>(PATHS.risk, { limit: 1 }, 10_000);
export const getExamples = () => serverGet<AgentExamples>(PATHS.agentExamples);
export const getFireHours = () => serverGet<FireHours>(PATHS.firesHours, { days: 7 });
export const getDelhiMet = () => serverGet<MetNow>(PATHS.metNow, { city: 'Delhi' });

/** How many case slips the showcase stands up. */
export const SLIPS = 6;

/**
 * Candidate unseen burns for the slips: the API's most confident unseen scars, one per village so the
 * six slips are six places, largest first. Then each one's case file, for its picture.
 */
export async function getCases(): Promise<{ list: ApiResult<ScarList>; cases: ScarCase[] }> {
  const list = await serverGet<ScarList>(PATHS.scars, { unseen: true, min_confidence: 0.9, limit: 120 }, 10_000);
  if (!list.ok) return { list, cases: [] };
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
  return { list, cases };
}
