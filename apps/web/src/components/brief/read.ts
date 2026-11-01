/**
 * Small readers for the field brief: the URL's window and CSV language, district slugs, the CSV
 * links, and the two qualifications every figure of the brief carries (the audit of candidate unseen
 * burns, the backtest of the burn-risk ranking), worded from the API's numbers, never from ours.
 * No server imports, so client and server components can both use it.
 */

import { absoluteApiUrl, apiUrl } from '@/lib/api';
import { dateIST, MISSING, NBSP, number, percent } from '@/lib/format';

import type { BriefAudit, BriefWindow, CsvLang, RankingReliability } from './types';

/** The windows offered on screen. The API takes 1 to 30 days; a link with another value still works. */
export const DAY_CHOICES = [3, 7, 14, 30] as const;
export const DEFAULT_DAYS = 7;
/** Rows per list on screen (the API's default); the CSV always has every village. */
export const ROW_LIMIT = 50;
/** The API's most rows per list: the whole watchlist, which no CSV carries, on ?watch=all. */
export const ALL_LIMIT = 2000;

type Param = string | string[] | undefined;
const first = (v: Param) => (Array.isArray(v) ? v[0] : v);

/** `?days=` as a whole number from 1 to 30; anything else is the default week. */
export function parseDays(v: Param): number {
  const n = Number(first(v));
  return Number.isInteger(n) && n >= 1 && n <= 30 ? n : DEFAULT_DAYS;
}

export function parseLang(v: Param): CsvLang {
  const s = first(v);
  return s === 'hi' || s === 'pa' ? s : 'en';
}

/** `?watch=all`: the district's whole watchlist instead of its first ROW_LIMIT villages. */
export const parseWatchAll = (v: Param): boolean => first(v) === 'all';

/** A path segment as the reader typed it; Next may hand it over still percent-encoded. */
export function decodeSegment(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/** "S.A.S Nagar" -> "sas-nagar". The API ignores case and punctuation in a district's name, so a
 *  slug finds the same district and keeps dots and spaces out of the address. */
export function districtSlug(name: string): string {
  return name
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[.'’]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function withQuery(path: string, q: Record<string, string | number | null | undefined>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v != null && v !== '') qs.set(k, String(v));
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

/** The index at a window (and CSV language); the defaults stay out of the address. */
export const indexHref = (days: number, lang: CsvLang = 'en') =>
  withQuery('/brief', { days: days === DEFAULT_DAYS ? null : days, lang: lang === 'en' ? null : lang });

export const districtHref = (name: string, days: number, watchAll = false) =>
  withQuery(`/brief/${districtSlug(name) || encodeURIComponent(name)}`, { days: days === DEFAULT_DAYS ? null : days, watch: watchAll ? 'all' : null });

/** The API's CSV of one district in one language: its own `links.csv` (relative to the API) with
 *  `lang` set, so the address always matches the JSON the page shows. */
export function csvHref(csvPath: string | null | undefined, district: string, days: number, lang: CsvLang): string {
  const fallback = apiUrl(`/api/brief/${encodeURIComponent(district)}/csv`, { days });
  const abs = absoluteApiUrl(csvPath) ?? fallback;
  try {
    const u = new URL(abs);
    u.searchParams.set('lang', lang);
    return u.toString();
  } catch {
    return `${fallback}&lang=${lang}`;
  }
}

/** The name the file is saved under: CsvDownload.tsx saves it from a same-origin blob, which honours
 *  `download` (the plain cross-origin link, before JavaScript, keeps the API's own name from
 *  Content-Disposition). */
export const csvFileName = (district: string, w: BriefWindow | null, lang: CsvLang) =>
  `blindspot-brief-${districtSlug(district)}${w ? `-${w.from}-to-${w.to}` : ''}-${lang}.csv`;

// ---------------------------------------------------------------------------------------------
// dates: the window's days are UTC dates of Sentinel-2 passes (UTC midnight is 05:30 IST the same day)

const dayOf = (iso: string) => {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  return Number.isFinite(d.getTime()) ? d : null;
};

/** "30 Oct" or "30 Oct 2026" from "2026-10-30" (or a full timestamp's date). */
export function passDate(iso: string | null | undefined, opts: { year?: boolean } = {}): string {
  const d = iso ? dayOf(iso) : null;
  return d ? dateIST(d, opts) : MISSING;
}

/** "24–30 Oct 2026", "28 Sep – 4 Oct 2026", or one day. */
export function windowRange(w: BriefWindow): string {
  const a = dayOf(w.from);
  const b = dayOf(w.to);
  if (!a || !b) return `${w.from} to ${w.to}`;
  if (w.from === w.to) return dateIST(b, { year: true });
  const sameYear = a.getUTCFullYear() === b.getUTCFullYear();
  if (sameYear && a.getUTCMonth() === b.getUTCMonth()) return `${a.getUTCDate()}–${dateIST(b, { year: true })}`;
  return `${dateIST(a, { year: !sameYear })} – ${dateIST(b, { year: true })}`;
}

/** "the 7 days of passes up to 30 Oct 2026" (the contract's own phrasing: never "the last 7 days"). */
export const windowWords = (w: BriefWindow) => `the ${w.days === 1 ? 'day' : `${w.days} days`} of passes up to ${passDate(w.to, { year: true })}`;

// ---------------------------------------------------------------------------------------------
// figures

/** Hectares with fixed decimals per column (0 for district totals, 1 for villages). */
export const ha = (v: number | null | undefined, decimals: 0 | 1 = 1) => number(v, { decimals });
export const haText = (v: number | null | undefined, decimals: 0 | 1 = 1) => `${ha(v, decimals)}${NBSP}ha`;
export const count = (v: number | null | undefined) => number(v);
/** The word for a count: `plural(1, 'burn')` is "burn", any other count "burns" (or `many`). */
export const plural = (n: number | null | undefined, one: string, many = `${one}s`) => (n === 1 ? one : many);

// A range never breaks at its dash (a word joiner after the en dash), and "n = 23" never splits.
const WJ = '⁠';
const ciText = (ci: [number, number] | null | undefined, f: (x: number) => string) =>
  ci && ci.length === 2 && ci.every((x) => Number.isFinite(x)) ? `${f(ci[0])}–${WJ}${f(ci[1])}` : null;

/** "audit precision 87% (95% CI 68–96%, n = 23)", or null when the API states no audit. */
export function auditPhrase(a: BriefAudit | null | undefined): string | null {
  if (!a || a.precision == null || !Number.isFinite(a.precision)) return null;
  const ci = ciText(a.precision_ci95, (x) => percent(x).replace('%', ''));
  const inner = [ci ? `95%${NBSP}CI ${ci}%` : null, a.audited != null ? `n${NBSP}=${NBSP}${number(a.audited)}` : null].filter(Boolean).join(', ');
  return `audit precision ${percent(a.precision)}${inner ? ` (${inner})` : ''}`;
}

/** The qualification beside every count and area of candidate unseen burns. */
export function auditSentence(a: BriefAudit | null | undefined): string {
  const phrase = auditPhrase(a);
  if (!phrase) return 'Candidate unseen burns are not confirmed fires, and the API states no audit of them yet: treat every count and area here as unchecked.';
  const dated = a?.current === false && a.date ? ` (an audit of ${passDate(a.date, { year: true })}, not of the current rule)` : '';
  return `Candidate unseen burns are not confirmed fires: their ${phrase}${dated}, so every count and area of them here is an upper bound.`;
}

/** The qualification beside every rank: how the ranking did in its backtest, never a probability. */
export function rankingSentence(r: RankingReliability | null | undefined): string {
  if (!r || !r.available) return 'The burn-risk ranking has no backtest yet, so its order is untested: it says where to look first, not the chance that a field will burn.';
  const auc = r.all_pairs?.auc_weighted_mean;
  const ci = ciText(r.all_pairs?.auc_ci95, (x) => number(x, { decimals: 3 }));
  const fit = auc != null ? ` (AUC ${number(auc, { decimals: 3 })}${ci ? `, 95%${NBSP}CI ${ci}` : ''}; 0.5 is chance)` : '';
  const strength = r.strength ? `${r.strength}` : 'untested';
  return `The top tenth comes from a burn-risk ranking that is ${strength} in its backtest${fit}: a rank says where to look first, not the chance that a field will burn.`;
}

/** "Weak ranking", for the pill beside the watchlist; null when the API states no strength. */
export const strengthLabel = (r: RankingReliability | null | undefined) =>
  r?.available && r.strength ? `${r.strength.charAt(0).toUpperCase()}${r.strength.slice(1)} ranking` : r && !r.available ? 'Untested ranking' : null;
