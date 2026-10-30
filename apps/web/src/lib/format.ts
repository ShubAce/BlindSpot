/**
 * Display formatting: IST times and dates, durations, numbers and units.
 *
 * Every time in the API is UTC with a Z; IST is a display concern (architecture.md section 5). IST has
 * no daylight saving, so dates are formatted from the fixed +05:30 offset with our own month and
 * weekday names: server and browser produce byte-identical strings (no hydration mismatch from ICU
 * differences) and nothing depends on the viewer's own time zone.
 *
 * Numbers use Indian digit grouping (en-IN: 1,23,456), a true minus sign (U+2212) and a fixed number
 * of decimals per metric, so columns of tabular figures line up. Units are returned as separate
 * strings so a page can set them smaller or greyer than the value. A missing value is an em dash,
 * which pages should render in the muted ink (app-screens.md section 11), never at full value size.
 */

export const LOCALE = 'en-IN';
export const IST_TIME_ZONE = 'Asia/Kolkata';
export const MINUS = '−';
/** Shown for a missing or not-applicable value. Render it in the muted ink. */
export const MISSING = '—';
/** Between a number and its unit: never let "9" and "h" fall on different lines. */
export const NBSP = ' ';

const IST_OFFSET_MS = 5.5 * 3600 * 1000;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export type DateInput = Date | string | number | null | undefined;

/** A Date from an ISO string, a Date, or epoch milliseconds. Unix seconds (smoke `times`) go through
 *  `fromUnixSeconds` instead, so a number is never guessed. Returns null for anything unparseable. */
export function toDate(v: DateInput): Date | null {
  if (v == null || v === '') return null;
  const d = v instanceof Date ? v : new Date(v);
  return Number.isFinite(d.getTime()) ? d : null;
}

export const fromUnixSeconds = (s: number): Date => new Date(s * 1000);

const pad2 = (n: number) => String(n).padStart(2, '0');

/** The wall-clock parts of an instant in IST. */
export function istParts(v: DateInput) {
  const d = toDate(v);
  if (!d) return null;
  const s = new Date(d.getTime() + IST_OFFSET_MS);
  return {
    year: s.getUTCFullYear(),
    month: s.getUTCMonth() + 1,
    day: s.getUTCDate(),
    hour: s.getUTCHours(),
    minute: s.getUTCMinutes(),
    second: s.getUTCSeconds(),
    weekday: s.getUTCDay(),
  };
}

/** "13:12" (24-hour, the default) or "1:12 PM". `suffix` appends " IST". */
export function timeIST(v: DateInput, opts: { hour12?: boolean; seconds?: boolean; suffix?: boolean } = {}): string {
  const p = istParts(v);
  if (!p) return MISSING;
  const sec = opts.seconds ? `:${pad2(p.second)}` : '';
  const out = opts.hour12
    ? `${p.hour % 12 || 12}:${pad2(p.minute)}${sec}${NBSP}${p.hour < 12 ? 'AM' : 'PM'}`
    : `${pad2(p.hour)}:${pad2(p.minute)}${sec}`;
  return opts.suffix ? `${out}${NBSP}IST` : out;
}

/** "30 Oct", with options "Fri 30 Oct" or "30 Oct 2026". */
export function dateIST(v: DateInput, opts: { weekday?: boolean; year?: boolean } = {}): string {
  const p = istParts(v);
  if (!p) return MISSING;
  const core = `${p.day}${NBSP}${MONTHS[p.month - 1]}${opts.year ? ` ${p.year}` : ''}`;
  return opts.weekday ? `${WEEKDAYS[p.weekday]} ${core}` : core;
}

/** "30 Oct, 13:12". */
export function dateTimeIST(v: DateInput, opts: { hour12?: boolean; year?: boolean; suffix?: boolean } = {}): string {
  const d = toDate(v);
  if (!d) return MISSING;
  return `${dateIST(d, { year: opts.year })}, ${timeIST(d, { hour12: opts.hour12, suffix: opts.suffix })}`;
}

/** "2026-10-30": the IST calendar day, for grouping and keys. */
export function istDayKey(v: DateInput): string {
  const p = istParts(v);
  return p ? `${p.year}-${pad2(p.month)}-${pad2(p.day)}` : '';
}

/** The instant of IST midnight that starts the IST day containing `v`. */
export function startOfIstDay(v: DateInput): Date | null {
  const d = toDate(v);
  if (!d) return null;
  const shifted = d.getTime() + IST_OFFSET_MS;
  return new Date(shifted - (((shifted % 86_400_000) + 86_400_000) % 86_400_000) - IST_OFFSET_MS);
}

/** Minutes since IST midnight, 0 to 1440 (fractional). */
export function istMinutesOfDay(v: DateInput): number | null {
  const d = toDate(v);
  const start = startOfIstDay(d);
  return d && start ? (d.getTime() - start.getTime()) / 60_000 : null;
}

/** "9 h 41 min", "6 min", "45 s", "2 d 4 h". Rounds down to the unit shown, so a countdown never
 *  promises more time than is left. Negative input is treated as its magnitude. */
export function duration(ms: number): string {
  if (!Number.isFinite(ms)) return MISSING;
  const s = Math.floor(Math.abs(ms) / 1000);
  if (s < 60) return `${s}${NBSP}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}${NBSP}min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}${NBSP}h ${m % 60}${NBSP}min` : `${h}${NBSP}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}${NBSP}d ${h % 24}${NBSP}h` : `${d}${NBSP}d`;
}

/** "just now", "4 min ago", "2 h ago", "3 d ago"; a future instant reads "in 9 h 41 min". */
export function relative(v: DateInput, now: Date): string {
  const d = toDate(v);
  if (!d) return MISSING;
  const diff = now.getTime() - d.getTime();
  if (diff >= 0 && diff < 45_000) return 'just now';
  if (diff < 0) return `in ${duration(-diff)}`;
  const m = Math.floor(diff / 60_000);
  if (m < 60) return `${Math.max(1, m)}${NBSP}min ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}${NBSP}h ago`;
  return `${Math.floor(h / 24)}${NBSP}d ago`;
}

// ---------------------------------------------------------------------------------------------
// numbers

const formatters = new Map<string, Intl.NumberFormat>();
function nf(decimals: number, compact = false): Intl.NumberFormat {
  const key = `${decimals}|${compact}`;
  let f = formatters.get(key);
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, compact
      ? { notation: 'compact', maximumFractionDigits: decimals }
      : { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    formatters.set(key, f);
  }
  return f;
}

export const isMissing = (v: unknown): v is null | undefined => v == null || (typeof v === 'number' && !Number.isFinite(v));

/** A number with Indian grouping and exactly `decimals` decimals; a true minus; "+" when `signed`. */
export function number(v: number | null | undefined, opts: { decimals?: number; signed?: boolean; compact?: boolean } = {}): string {
  if (isMissing(v)) return MISSING;
  const decimals = opts.decimals ?? 0;
  const body = nf(decimals, opts.compact).format(Math.abs(v));
  // A value that rounds to zero carries no sign: "0", never "-0" or "+0".
  const zero = Number(Math.abs(v).toFixed(decimals)) === 0;
  const sign = zero ? '' : v < 0 ? MINUS : opts.signed ? '+' : '';
  return sign + body;
}

/** A 0-1 share as a percentage: 0.962 -> "96%" (or "96.2%" with decimals 1). */
export function percent(share: number | null | undefined, decimals = 0): string {
  return isMissing(share) ? MISSING : `${number(share * 100, { decimals })}%`;
}

/** Display rules per metric: fixed decimals and the unit, kept apart from the value. `wholeAbove`
 *  drops the decimals once a value is large enough that they are noise (a 0.6 ha field keeps one
 *  decimal; a 12,480 ha district total does not). `scale` multiplies first (a 0-1 share -> %). */
export const METRICS = {
  count: { decimals: 0, unit: '' },
  frp_mw: { decimals: 1, unit: 'MW', wholeAbove: 100 },
  area_ha: { decimals: 1, unit: 'ha', wholeAbove: 100 },
  pm25: { decimals: 0, unit: 'µg/m³' },
  pm10: { decimals: 0, unit: 'µg/m³' },
  aqi: { decimals: 0, unit: '' },
  km: { decimals: 0, unit: 'km' },
  m: { decimals: 0, unit: 'm' },
  blh_m: { decimals: 0, unit: 'm' },
  wind_kmh: { decimals: 0, unit: 'km/h' },
  wind_ms: { decimals: 1, unit: 'm/s' },
  temp_c: { decimals: 1, unit: '°C' },
  rain_mm: { decimals: 1, unit: 'mm' },
  pct: { decimals: 0, unit: '%' },
  share: { decimals: 0, unit: '%', scale: 100 },
  deg: { decimals: 0, unit: '°' },
  dnbr: { decimals: 2, unit: '' },
  smoke_index: { decimals: 1, unit: '' },
  pixel_hours: { decimals: 0, unit: 'pixel-hours' },
  risk: { decimals: 2, unit: '' },
} as const satisfies Record<string, { decimals: number; unit: string; wholeAbove?: number; scale?: number }>;

export type MetricKey = keyof typeof METRICS;

/** `{ value: "312", unit: "µg/m³" }`: set the unit smaller or muted beside the value. */
export function metric(key: MetricKey, v: number | null | undefined, opts: { signed?: boolean } = {}): { value: string; unit: string } {
  const m: { decimals: number; unit: string; wholeAbove?: number; scale?: number } = METRICS[key];
  if (isMissing(v)) return { value: MISSING, unit: '' };
  const x = v * (m.scale ?? 1);
  const decimals = m.wholeAbove != null && Math.abs(x) >= m.wholeAbove ? 0 : m.decimals;
  return { value: number(x, { decimals, signed: opts.signed }), unit: m.unit };
}

/** "312 µg/m³" as one string, the number and unit joined by a no-break space. */
export function metricText(key: MetricKey, v: number | null | undefined, opts: { signed?: boolean } = {}): string {
  const { value, unit } = metric(key, v, opts);
  if (!unit || value === MISSING) return value;
  return unit === '%' || unit === '°' || unit === '°C' ? `${value}${unit}` : `${value}${NBSP}${unit}`;
}

const COMPASS_8 = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const COMPASS_WORDS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];

/** 8-point compass for a bearing in degrees: 315 -> "NW". */
export function compass(deg: number | null | undefined): string {
  if (isMissing(deg)) return MISSING;
  return COMPASS_8[Math.round((((deg % 360) + 360) % 360) / 45) % 8];
}

/** "north-west" for 315: for sentences like "wind from the north-west". */
export function compassWords(deg: number | null | undefined): string {
  if (isMissing(deg)) return MISSING;
  return COMPASS_WORDS[Math.round((((deg % 360) + 360) % 360) / 45) % 8];
}

/** "Sangrur, Punjab" from optional parts, skipping blanks. */
export const placeName = (...parts: (string | null | undefined)[]): string =>
  parts.filter((p): p is string => !!p && p.trim() !== '').join(', ');
