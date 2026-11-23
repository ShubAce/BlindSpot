import 'server-only';

import { PATHS } from '@/lib/api';
import { REFERENCE_SITE } from '@/lib/aoi';
import { serverGet, type ApiResult } from '@/lib/api-server';
import { dateIST } from '@/lib/format';

import type { AgentExamples, Diurnal, Featured, FireHours, FrameScars, Headline, MetNow, OrbitPasses, Risk, ScarCase, ScarList, ScarListProps } from './types';

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

/** The newest list: the most recently sensed candidate unseen burns at this confidence or more (the
 *  API lists the newest scar first, by exact sensing time, so 120 rows can be one or two tiles of a
 *  pass). The sitemap's case pages, and the slips' pass (the day of its first scar). */
export const UNSEEN_LIST_QUERY = { unseen: true, min_confidence: 0.9, limit: 120 };

/** The API's most recently sensed candidate unseen burns: the sitemap's case pages (app/sitemap.ts),
 *  and the read that names the newest pass for the slips. */
export const getUnseenList = () => serverGet<ScarList>(PATHS.scars, UNSEEN_LIST_QUERY, 15_000);

/** Up to `n` of the list's unseen scars, largest first and one per village, so they are `n` places. */
export function onePerPlace(list: ScarList, n: number): ScarListProps[] {
  const seen = new Set<string>();
  return list.features.features
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
    .slice(0, n);
}

/** What the slips are drawn from, for the line that names it. `passes`: the IST dates of every listed
 *  scar (one date: one pass).
 *  - 'pass': the newest pass's list, largest first; `matched` scars of that pass qualify in all.
 *  - 'newest': the newest list's `returned` scars, the most recently sensed (an API before its
 *    `order` parameter, or a pass read that failed). They are not the pass's largest. */
export type SlipScope = { passes: string[] } & ({ kind: 'pass'; matched: number } | { kind: 'newest'; returned: number });

/**
 * The slips' list: the newest pass (the UTC day of the newest list's first scar) at the same
 * confidence, largest first (`order=area`), so a cut list keeps the pass's largest scars. An answer
 * that does not echo `order: 'area'` was listed newest first; the newest list then stands in, and
 * its scope says what it is.
 */
export async function getSlipList(): Promise<{ list: ApiResult<ScarList>; scope: SlipScope }> {
  const newest = await getUnseenList();
  const rows = newest.ok ? newest.data.features.features : [];
  if (rows.length) {
    const day = new Date(rows[0].properties.burn_before).toISOString().slice(0, 10);
    const pass = await serverGet<ScarList>(PATHS.scars, { ...UNSEEN_LIST_QUERY, since: day, until: day, order: 'area' }, 15_000);
    if (pass.ok && pass.data.order === 'area' && pass.data.totals) {
      return { list: pass, scope: { kind: 'pass', passes: passesOf(pass.data.features.features), matched: pass.data.totals.unseen_clusters } };
    }
  }
  return { list: newest, scope: { kind: 'newest', passes: passesOf(rows), returned: rows.length } };
}

/** The IST dates of the listed scars' passes, each once. */
const passesOf = (rows: ScarList['features']['features']) => [...new Set(rows.map((f) => dateIST(f.properties.burn_before)))];

/** The slips' scars (onePerPlace of the slips' list) without their case files, and what they are
 *  drawn from, for the lines that only name them; none when the list did not load (the slips then
 *  say so in their own place). */
export async function getSlipScars(): Promise<{ shown: ScarListProps[]; scope: SlipScope }> {
  const { list, scope } = await getSlipList();
  return { shown: list.ok ? onePerPlace(list.data, SLIPS) : [], scope };
}

/**
 * Candidate unseen burns for the slips: six places (onePerPlace), then each one's case file, for its
 * picture. `failed`: a read behind them failed (the list, or a case file, whose slip then has no
 * picture), so the page carries the failure mark (components/pwa/LoadFailed) and the offline worker
 * keeps its good copy instead of this one.
 */
export async function getCases(): Promise<{ list: ApiResult<ScarList>; cases: ScarCase[]; failed: boolean }> {
  const { list } = await getSlipList();
  if (!list.ok) return { list, cases: [], failed: true };
  const picked = onePerPlace(list.data, SLIPS);
  const files = await Promise.all(picked.map((p) => serverGet<ScarCase>(PATHS.scarCase(p.id))));
  const cases = picked.map((p, i) => {
    const r = files[i];
    return r.ok ? r.data : ({ case: p, imagery: null, generated_at: list.data.generated_at, sources: [] } as ScarCase);
  });
  return { list, cases, failed: files.some((r) => !r.ok) };
}
