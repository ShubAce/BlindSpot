/**
 * Small readers shared by the case file, the bulletin, the method page and the field brief.
 *
 * - `auditPhrase` is the one way the site states the audit precision of candidate unseen burns,
 *   which must stand beside every count, area or share of them (contract notes, "unseen_burns"), and
 *   `auditBrackets` the interval, sample size and audit's rule that follow the precision on every page.
 *   The figures are read from /api/stats/headline; nothing here holds a number.
 * - `platformName` turns a Sentinel-2 item id ("S2B_T43REP_20261028T054808_L2A" or
 *   "S2C_MSIL2A_...") into the satellite's name, so a pass reads "Sentinel-2B".
 * - `caseState` mirrors `scarState` in components/map/layers/scars.ts. It is repeated here, three
 *   lines, so the case and method routes do not import that module and with it deck.gl.
 */

import { dateIST, percent } from '@/lib/format';

import type { UnseenBurnsBlock } from './types';

export type CaseState = 'unseen' | 'alerted' | 'unchecked';
export const caseState = (unseen: boolean | null | undefined): CaseState => (unseen === true ? 'unseen' : unseen === false ? 'alerted' : 'unchecked');

/** The audit's own rule and day, as /api/stats/headline states them beside the precision. */
export type AuditOf = { audit_date?: string | null; audit_model_version?: string | null; audit_current?: boolean | null };

/** True when the audit judged an earlier rule than the one that mapped the scars (audit_current false). */
export const auditIsOlder = (u: AuditOf | null | undefined): boolean => u?.audit_current === false;

/** "an audit of 31 Oct 2026 of the earlier rule s2burn-rules-2, not of the current rule" when the
 *  audit judged an earlier rule (the field brief says the same, brief/read.ts); null otherwise. */
export function auditOlderText(u: AuditOf | null | undefined): string | null {
  if (!auditIsOlder(u)) return null;
  const day = u?.audit_date ? ` of ${dateIST(`${u.audit_date}T12:00:00+05:30`, { year: true })}` : '';
  const rule = u?.audit_model_version ? ` ${u.audit_model_version}` : '';
  return `an audit${day} of the earlier rule${rule}, not of the current rule`;
}

/** The brackets after an audit precision, " (95% CI 61%–94%, n = 18)", with "; an audit of 31 Oct
 *  2026 of the earlier rule …" inside them when the audit judged an earlier rule; "" when there is
 *  nothing to put in them. Every page that states the precision builds its brackets here. `n` writes
 *  the sample size (as the number is, by default); `ciMissing` stands in for an interval the API did
 *  not give ("95% CI not given") instead of leaving the interval out; `space` goes inside "95% CI"
 *  and around the "=" of "n = " (mission control's panels keep them together with NBSP). */
export function auditBrackets(
  u: ({ precision_ci95?: [number, number] | null; precision_n?: number | null } & AuditOf) | null | undefined,
  opts: { n?: (n: number) => string; ciMissing?: string; space?: string } = {},
): string {
  const sp = opts.space ?? ' ';
  const range = u?.precision_ci95 && u.precision_ci95.length === 2 ? `${percent(u.precision_ci95[0])}–${percent(u.precision_ci95[1])}` : opts.ciMissing;
  const ci = range != null ? `95%${sp}CI ${range}` : null;
  const n = u?.precision_n != null ? `n${sp}=${sp}${opts.n ? opts.n(u.precision_n) : u.precision_n}` : null;
  const inner = [[ci, n].filter(Boolean).join(', '), auditOlderText(u)].filter(Boolean).join('; ');
  return inner ? ` (${inner})` : '';
}

/** "audit precision 83% (95% CI 61%–94%, n = 18)" (with `verb`: "audit precision was 83% (…)", for a
 *  sentence), or null when the API has no audit to state. An audit of an earlier rule says so inside
 *  the brackets: "(95% CI 61%–94%, n = 18; an audit of 31 Oct 2026 of the earlier rule …)". */
export function auditPhrase(u: UnseenBurnsBlock | null | undefined, verb?: string): string | null {
  if (!u || u.precision == null) return null;
  return `audit precision ${verb ? `${verb} ` : ''}${percent(u.precision)}${auditBrackets(u)}`;
}

/** "Sentinel-2B" from a Sentinel-2 item id; null when the id does not start with a platform code. */
export function platformName(itemId: string | null | undefined): string | null {
  const m = itemId?.match(/^S2([A-D])[_]/i);
  return m ? `Sentinel-2${m[1].toUpperCase()}` : null;
}

/** An answer's sources with each input once: the API can list the same key and URL twice. */
export const uniqueSources = <T extends { key: string; url: string | null }>(sources: readonly T[] | null | undefined): T[] =>
  Array.from(new Map((sources ?? []).map((s) => [`${s.key}|${s.url}`, s])).values());

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
