/**
 * Small readers shared by the case file, the bulletin and the method page.
 *
 * - `auditPhrase` is the one way the site states the audit precision of candidate unseen burns,
 *   which must stand beside every count, area or share of them (contract notes, "unseen_burns").
 *   The figures are read from /api/stats/headline; nothing here holds a number.
 * - `platformName` turns a Sentinel-2 item id ("S2B_T43REP_20261028T054808_L2A" or
 *   "S2C_MSIL2A_...") into the satellite's name, so a pass reads "Sentinel-2B".
 * - `caseState` mirrors `scarState` in components/map/layers/scars.ts. It is repeated here, three
 *   lines, so the case and method routes do not import that module and with it deck.gl.
 */

import { percent } from '@/lib/format';

import type { UnseenBurnsBlock } from './types';

export type CaseState = 'unseen' | 'alerted' | 'unchecked';
export const caseState = (unseen: boolean | null | undefined): CaseState => (unseen === true ? 'unseen' : unseen === false ? 'alerted' : 'unchecked');

/** "audit precision 87% (95% CI 68–96%, n = 23)", or null when the API has no audit to state. */
export function auditPhrase(u: UnseenBurnsBlock | null | undefined): string | null {
  if (!u || u.precision == null) return null;
  const ci = u.precision_ci95 && u.precision_ci95.length === 2 ? `95% CI ${percent(u.precision_ci95[0])}–${percent(u.precision_ci95[1])}` : null;
  const n = u.precision_n != null ? `n = ${u.precision_n}` : null;
  const inner = [ci, n].filter(Boolean).join(', ');
  return `audit precision ${percent(u.precision)}${inner ? ` (${inner})` : ''}`;
}

/** "Sentinel-2B" from a Sentinel-2 item id; null when the id does not start with a platform code. */
export function platformName(itemId: string | null | undefined): string | null {
  const m = itemId?.match(/^S2([A-D])[_]/i);
  return m ? `Sentinel-2${m[1].toUpperCase()}` : null;
}

/** "earth-search.aws.element84.com/v1/collections/…" for a long source URL: host plus a short path. */
export function shortUrl(u: string | null | undefined, max = 64): string | null {
  if (!u) return null;
  try {
    const url = new URL(u);
    const s = `${url.host}${url.pathname === '/' ? '' : url.pathname}`;
    return s.length > max ? `${s.slice(0, max - 1)}…` : s;
  } catch {
    return u.length > max ? `${u.slice(0, max - 1)}…` : u;
  }
}
