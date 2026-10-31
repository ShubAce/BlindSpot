/**
 * The blind-spot clock's model: a pure computation over API responses, shared by the server render
 * (the no-script picture) and the browser (which only moves the hand and the live line).
 *
 *   - gold marks: the times of day a polar fire satellite (VIIRS, MODIS) looked at Punjab and Haryana
 *     over the last days, from GET /api/geostationary/diurnal `polar_windows_ist` (built from the
 *     satellites' own FIRMS detections). The legend's share outside the polar looks is measured by the
 *     API against exactly these windows;
 *   - the bite: of the spans between those windows (`unobserved_gaps_ist`), the one that holds the most
 *     GK-2A fire activity: the API's own blind spot, the same span as the headline's
 *     `gk2a_evening.unobserved_span_ist`;
 *   - red bars: GK-2A fire activity by IST hour, the mean of the same days (`hourly_ist`), an activity
 *     index, not a count of fires.
 *
 * If the diurnal answer is missing, the dial falls back to today's computed passes over the reference
 * site (GET /api/orbits/passes), bites the longest stretch with no pass, and says so.
 *
 * Nothing here supplies a value: an empty input gives an empty part, and the caller says so.
 */

import { startOfIstDay } from '@/lib/format';

import type { Diurnal, OrbitPass } from './types';

const DAY_MIN = 1440;
const MIN_MS = 60_000;

export type ClockLook = { key: string; who: string; from: number; to: number };
export type ClockGap = {
  from: number;
  to: number;
  minutes: number;
  /** GK-2A activity inside the span (pixel-hours) and its share of all of it; region scope only */
  pixelHours: number | null;
  share: number | null;
};
export type ClockBar = { hour: number; value: number };

export type ClockModel = {
  /** 'region': the API's polar windows over Punjab and Haryana; 'site': today's passes over the site */
  scope: 'region' | 'site';
  /** IST midnight that starts the day the hand runs on (ms). */
  dayStart: number;
  /** The looks, minutes after IST midnight (0..1440), sorted. */
  looks: ClockLook[];
  /** Minutes of the day inside at least one look. */
  lookMinutes: number;
  /** The span bitten from the ring; `to` may be smaller than `from` (it wraps). */
  gap: ClockGap | null;
  bars: ClockBar[];
  barMax: number;
};

/** Every pass in the window, as instants, for the live line (who is overhead, who is next). */
export type LivePass = { satellite: string; start: number; end: number };

export function livePasses(passes: OrbitPass[] | undefined): LivePass[] {
  return (passes ?? [])
    .map((p) => ({ satellite: p.satellite, start: Date.parse(p.start_utc), end: Date.parse(p.end_utc) }))
    .filter((p) => Number.isFinite(p.start) && Number.isFinite(p.end) && p.end >= p.start)
    .sort((a, b) => a.start - b.start);
}

/** "16:30" -> 990; null if it is not a time of day. */
function minuteOf(hhmm: string | null | undefined): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm ?? '');
  if (!m) return null;
  const v = Number(m[1]) * 60 + Number(m[2]);
  return v >= 0 && v <= DAY_MIN ? v : null;
}

function merge(looks: ClockLook[]): [number, number][] {
  const merged: [number, number][] = [];
  for (const l of looks) {
    const last = merged[merged.length - 1];
    if (last && l.from <= last[1]) last[1] = Math.max(last[1], l.to);
    else merged.push([l.from, l.to]);
  }
  return merged;
}

type GapRow = NonNullable<Diurnal['unobserved_gaps_ist']>[number];

/** The API's polar windows and the gap with the most GK-2A activity in it (its blind spot). */
function regionLooks(d: Diurnal): { looks: ClockLook[]; gap: ClockGap | null } | null {
  const looks: ClockLook[] = (d.polar_windows_ist ?? [])
    .filter((w) => Number.isFinite(w.start_min) && Number.isFinite(w.end_min) && w.end_min >= w.start_min)
    .map((w) => ({ key: `${w.start_min}-${w.end_min}`, who: (w.satellites ?? []).join(', '), from: Math.max(0, w.start_min), to: Math.min(DAY_MIN, w.end_min) }))
    .sort((a, b) => a.from - b.from);
  if (!looks.length) return null;
  const gaps: { g: GapRow; from: number }[] = [];
  for (const g of d.unobserved_gaps_ist ?? []) {
    const from = minuteOf(g.start);
    if (from != null && Number.isFinite(g.minutes) && g.minutes > 0) gaps.push({ g, from });
  }
  // the API's own pick (headline gk2a_evening.unobserved_span_ist): the most activity; else the longest
  const fire = (x: { g: GapRow }) => x.g.pixel_hours ?? 0;
  let best: { g: GapRow; from: number } | null = null;
  for (const x of gaps) {
    if (!best || fire(x) > fire(best) || (fire(x) === fire(best) && x.g.minutes > best.g.minutes)) best = x;
  }
  const total = d.pixel_hours ?? null;
  const gap: ClockGap | null = best
    ? {
        from: best.from,
        to: (best.from + best.g.minutes) % DAY_MIN,
        minutes: best.g.minutes,
        pixelHours: best.g.pixel_hours ?? null,
        share: best.g.pixel_hours != null && total ? best.g.pixel_hours / total : null,
      }
    : null;
  return { looks, gap };
}

/** Today's passes over the site and the longest stretch with none, read round the dial. */
function siteLooks(passes: OrbitPass[] | undefined, dayStart: number): { looks: ClockLook[]; gap: ClockGap | null } {
  const dayEnd = dayStart + DAY_MIN * MIN_MS;
  const looks: ClockLook[] = (passes ?? [])
    .map((p) => ({ p, a: Date.parse(p.start_utc), b: Date.parse(p.end_utc) }))
    .filter(({ a, b }) => Number.isFinite(a) && Number.isFinite(b) && b >= a && b > dayStart && a < dayEnd)
    .map(({ p, a, b }) => ({
      key: p.start_utc + p.satellite,
      who: p.satellite,
      from: Math.max(0, (a - dayStart) / MIN_MS),
      to: Math.min(DAY_MIN, (b - dayStart) / MIN_MS),
    }))
    .sort((x, y) => x.from - y.from);
  const merged = merge(looks);
  let gap: ClockGap | null = null;
  for (let i = 0; i < merged.length; i++) {
    const from = merged[i][1];
    const nextStart = i + 1 < merged.length ? merged[i + 1][0] : merged[0][0] + DAY_MIN;
    const minutes = nextStart - from;
    if (minutes > 0 && (!gap || minutes > gap.minutes)) gap = { from, to: nextStart % DAY_MIN, minutes, pixelHours: null, share: null };
  }
  return { looks, gap };
}

/**
 * The model for the IST day containing `at` (an instant from the data: the passes' `generated_at`, so
 * the server render is the same for everyone who receives it).
 */
export function clockModel(passes: OrbitPass[] | undefined, diurnal: Diurnal | null, at: string | number): ClockModel | null {
  const start = startOfIstDay(at);
  if (!start) return null;
  const dayStart = start.getTime();
  const region = diurnal ? regionLooks(diurnal) : null;
  const { looks, gap } = region ?? siteLooks(passes, dayStart);
  const lookMinutes = merge(looks).reduce((t, [a, b]) => t + (b - a), 0);

  const bars: ClockBar[] = (diurnal?.hourly_ist ?? [])
    .filter((h) => h.mean_count != null && Number.isFinite(h.mean_count) && h.hour >= 0 && h.hour < 24)
    .map((h) => ({ hour: h.hour, value: h.mean_count as number }));
  const barMax = bars.reduce((m, b) => Math.max(m, b.value), 0);

  return { scope: region ? 'region' : 'site', dayStart, looks, lookMinutes, gap, bars, barMax };
}

/** True if minute-of-day `m` lies inside the gap (which may wrap past midnight). */
export function inGap(gap: ClockGap, m: number): boolean {
  const x = ((m % DAY_MIN) + DAY_MIN) % DAY_MIN;
  return gap.from <= gap.to ? x >= gap.from && x < gap.to : x >= gap.from || x < gap.to;
}

/** "20:40" for minutes after midnight (wrapping past 24 h). */
export function clockTime(min: number): string {
  const m = Math.round(((min % DAY_MIN) + DAY_MIN) % DAY_MIN) % DAY_MIN;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;  // a clock's hours and minutes; taste-skill-ignore
}

/** "5 h 01 min" for a stretch of minutes. */
export function clockSpan(min: number): { h: number; m: number } {
  const total = Math.round(min);
  return { h: Math.floor(total / 60), m: total % 60 };
}

// ---------------------------------------------------------------------------------------------
// geometry: the dial in a 480 x 480 box, midnight at the top, the day running clockwise

export const DIAL = { size: 480, c: 240, ringR: 199, ringW: 30, barIn: 112, barOut: 178, labelR: 96, handIn: 124, handOut: 212 } as const;

/** Angle in radians (0 at the top, clockwise) for minutes after midnight. */
export const angleOf = (min: number): number => (min / DAY_MIN) * 2 * Math.PI;

export function polar(r: number, a: number): [number, number] {
  return [DIAL.c + r * Math.sin(a), DIAL.c - r * Math.cos(a)];
}

const f = (n: number) => Math.round(n * 10) / 10;

/** An annular sector from minute a to minute b (b may pass 1440), between radii r0 and r1. */
export function sector(a: number, b: number, r0: number, r1: number): string {
  const span = b - a;
  const a0 = angleOf(a);
  const a1 = angleOf(b);
  const large = span > DAY_MIN / 2 ? 1 : 0;
  const [x0, y0] = polar(r1, a0);
  const [x1, y1] = polar(r1, a1);
  const [x2, y2] = polar(r0, a1);
  const [x3, y3] = polar(r0, a0);
  return `M${f(x0)} ${f(y0)}A${r1} ${r1} 0 ${large} 1 ${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}A${r0} ${r0} 0 ${large} 0 ${f(x3)} ${f(y3)}Z`;
}

/** An open arc at radius r from minute a to minute b (b may pass 1440). */
export function arc(a: number, b: number, r: number): string {
  const span = b - a;
  const [x0, y0] = polar(r, angleOf(a));
  const [x1, y1] = polar(r, angleOf(b));
  return `M${f(x0)} ${f(y0)}A${r} ${r} 0 ${span > DAY_MIN / 2 ? 1 : 0} 1 ${f(x1)} ${f(y1)}`;
}
