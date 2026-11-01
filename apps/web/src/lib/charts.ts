/**
 * Small, dependency-free SVG charts for app screens: a typed port of the design skill's charts.js
 * (.claude/skills/design-taste-frontend/assets/charts.js), same API, same rules
 * (app-screens.md section 6): one question per chart, one series that matters, 3-5 quiet horizontal
 * gridlines, round ticks that top out above the largest value, 2px straight lines with no markers,
 * square bars, no gradients, a bare tooltip, tabular figures. Colours come from CSS tokens read at
 * draw time: --chart-1 ... --chart-4, --chart-muted, --chart-mark, --chart-span, --chart-track,
 * --grid, --axis-ink, --ink, --surface, --raised, --rule, --negative (see app/globals.css).
 *
 * Differences from the original, all for React: every chart returns `destroy()` (disconnects its
 * resize observer, font and theme listeners), a chart draws itself again when the theme changes (its
 * colours are baked in at draw time), numbers use the app's locale (en-IN, like lib/format.ts), and
 * a label that does not fit warns once per element through a WeakMap. Use them through
 * components/charts/useChart.ts:
 *
 *   const ref = useChart((el) => Charts.line(el, { labels, series, format: Charts.fmt.int }), [labels, series]);
 *   <div ref={ref} className="chart" />
 */

import { LOCALE } from './format';
import { onThemeChange } from './theme';

type Fmt = (v: number) => string;
type AttrValue = string | number | null | undefined;
type Attrs = Record<string, AttrValue>;
type Label = string | number;
type LegendTarget = Element | string | null | undefined;

export type ChartHandle = { destroy: () => void };
export type ChartWithLegend = ChartHandle & { legend: HTMLUListElement | null };
export type ChartWithSvg = ChartWithLegend & { readonly svg: SVGSVGElement | null };

const NS = 'http://www.w3.org/2000/svg';
const MINUS = '−';
const ROW = 14;
const LANE = (rows: number) => 8 + rows * ROW + (rows ? 4 : 0);

const css = (el: Element, name: string, fb: string): string => getComputedStyle(el).getPropertyValue(name).trim() || fb;
const cssNum = (el: Element, name: string, fb: number): number => {
  const v = parseFloat(css(el, name, ''));
  return Number.isFinite(v) ? v : fb;
};

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Attrs = {}, parent?: Element | null): SVGElementTagNameMap[K] {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, String(v));
  if (parent) parent.appendChild(n);
  return n;
}
const text = (parent: Element, str: string, attrs: Attrs): SVGTextElement => {
  const t = svg('text', attrs, parent);
  t.textContent = str;
  return t;
};
const titled = <T extends Element>(parent: T, str: string): T => {
  svg('title', {}, parent).textContent = str;
  return parent;
};

const nfCache = new Map<string, Intl.NumberFormat>();
const nf = (o: Intl.NumberFormatOptions): Intl.NumberFormat => {
  const key = JSON.stringify(o);
  let f = nfCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, o);
    nfCache.set(key, f);
  }
  return f;
};

// every text value that reaches innerHTML goes through esc: a label from a database is text, never markup
const esc = (v: unknown): string =>
  String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c);

export const fmt = {
  int: (v: number) => nf({ maximumFractionDigits: 0 }).format(v),
  dec: (d: number): Fmt => (v) => nf({ minimumFractionDigits: d, maximumFractionDigits: d }).format(v),
  pct: (d = 1): Fmt => (v) => nf({ style: 'percent', minimumFractionDigits: d, maximumFractionDigits: d }).format(v),
  money: (cur = 'INR', d = 0): Fmt => (v) => nf({ style: 'currency', currency: cur, minimumFractionDigits: d, maximumFractionDigits: d }).format(v),
  compact: (v: number) => nf({ notation: 'compact', maximumFractionDigits: 1 }).format(v),
  ms: (v: number) => `${nf({ maximumFractionDigits: 0 }).format(v)} ms`,
  /** a sign on every change, with a true minus: signed() -> '+2', '0', '−1'; signed(dec(1)) -> '+0.4' */
  signed: (f: Fmt = (v) => nf({ maximumFractionDigits: 0 }).format(v)): Fmt => (v) => (v > 0 ? '+' : v < 0 ? MINUS : '') + f(Math.abs(v)),
  /** minutes since midnight as a 24-hour time: 680 -> '11:20' */
  clock: (m: number) => `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(Math.round(m % 60)).padStart(2, '0')}`,
};

const minutes = (t: string | number | null | undefined): number | null => {
  if (t == null || t === '') return null;
  if (typeof t === 'number') return t;
  const [hh, mm] = String(t).split(':').map(Number);
  return hh * 60 + (mm || 0);
};

/** Round ticks (steps of 1, 2, 2.5 or 5 x 10^n) whose top sits above the largest value. */
export function ticks(min: number, max: number, count = 4): number[] {
  if (max === min) max = min + 1;
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  let hi = Math.ceil(max / step) * step;
  if (hi <= max) hi += step;
  const out: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}

/**
 * x labels keep one step all the way: every k-th label, with k chosen so the labels fit the width,
 * and a step that lands on the last label when one gives enough labels. Never a forced end label
 * that breaks the step. Clock labels ('08:30') step in whole hours (or halves and quarters) from a
 * round time, so a half-hourly axis reads 06:00, 09:00.
 */
export function xPicks(labels: readonly Label[], span: number, at?: readonly number[] | null): number[] {
  const n = labels.length;
  if (at) return at.filter((i) => i >= 0 && i < n);
  if (n <= 1) return n ? [0] : [];
  const longest = Math.max(...labels.map((l) => String(l).length));
  const cap = Math.max(2, Math.min(8, Math.floor(span / (longest * 7 + 18))));
  if (n <= cap) return [...Array(n).keys()];
  const mins = labels.every((l) => /^\d{1,2}:\d{2}$/.test(String(l))) ? labels.map((l) => minutes(l) as number) : null;
  const d = mins ? mins[1] - mins[0] : 0;
  let first: { k: number; s: number } | null = null;
  let land: { k: number; s: number; count: number } | null = null;
  for (let k = 1; k < n; k++) {
    const stepM = k * d;
    const clock = !!mins && d > 0;
    if (clock && (stepM >= 60 ? ![1, 2, 3, 4, 6, 12].includes(stepM / 60) : 60 % stepM !== 0)) continue;
    // start on a round time: a multiple of the step (09:00 for three hours), or a whole hour for shorter steps
    let s = clock && mins ? mins.findIndex((m) => m % Math.max(60, stepM) === 0) : 0;
    if (s < 0 && mins) s = Math.max(0, mins.findIndex((m) => m % 60 === 0));
    const count = Math.floor((n - 1 - s) / k) + 1;
    if (count > cap) continue;
    if (first == null) first = { k, s };
    if ((n - 1 - s) % k === 0) {
      land = { k, s, count };
      break;
    }
  }
  const pick = land && land.count >= Math.max(3, Math.ceil(cap * 0.6)) ? land : first || { k: n - 1, s: 0 };
  const out: number[] = [];
  for (let i = pick.s; i < n; i += pick.k) out.push(i);
  return out;
}

// colours: tokens resolved to rgb, so a mark can be mixed with the surface (a pale fill, a darker step)
let swatchProbe: HTMLSpanElement | null = null;
function rgb(el: Element, value: string): number[] | null {
  if (!swatchProbe) {
    swatchProbe = document.createElement('span');
    swatchProbe.style.display = 'none';
  }
  el.appendChild(swatchProbe);
  swatchProbe.style.color = '';
  swatchProbe.style.color = value;
  const c = getComputedStyle(swatchProbe).color;
  swatchProbe.remove();
  const m = c.match(/-?[\d.]+/g);
  if (!m || m.length < 3) return null;
  return c.startsWith('color(') ? m.slice(0, 3).map((v) => Math.round(+v * 255)) : m.slice(0, 3).map(Number);
}
const mix = (el: Element, a: string, b: string, t: number): string => {
  const p = rgb(el, a);
  const q = rgb(el, b);
  return p && q ? `rgb(${p.map((v, i) => Math.round(v * t + q[i] * (1 - t))).join(',')})` : a;
};

// text width, measured in the chart's own face (falls back to an estimate when the chart is hidden)
function measure(s: SVGSVGElement, str: Label, size = 11, weight = 400): number {
  const t = text(s, String(str), { x: -999, y: -999, 'font-size': size, 'font-weight': weight, visibility: 'hidden' });
  let wd = 0;
  try {
    wd = t.getComputedTextLength();
  } catch {
    wd = 0;
  }
  t.remove();
  return wd > 0 ? wd : String(str).length * size * 0.56;
}
function fit(s: SVGSVGElement, input: Label, max: number, size?: number, weight?: number): string {
  let str = String(input);
  if (measure(s, str, size, weight) <= max) return str;
  while (str.length > 1 && measure(s, `${str}…`, size, weight) > max) str = str.slice(0, -1);
  return `${str.trimEnd()}…`;
}

function frame(el: HTMLElement, h: number): { s: SVGSVGElement; w: number; h: number } {
  el.innerHTML = '';
  const w = Math.max(240, el.clientWidth || 600);
  const s = svg('svg', { viewBox: `0 0 ${w} ${h}`, width: '100%', height: h, role: 'img', style: 'display:block;overflow:visible;font-variant-numeric:tabular-nums' }, el);
  return { s, w, h };
}

function tooltip(el: HTMLElement): HTMLDivElement {
  let t = el.querySelector<HTMLDivElement>('.chart-tip');
  if (!t) {
    t = document.createElement('div');
    t.className = 'chart-tip';
    t.style.cssText =
      'position:absolute;pointer-events:none;padding:6px 8px;font:inherit;font-size:12px;line-height:1.35;border-radius:6px;white-space:nowrap;opacity:0;transition:opacity 120ms ease;background:var(--surface,#fff);color:var(--ink,#111);border:1px solid var(--grid,#e5e7eb)';
    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
    el.appendChild(t);
  }
  return t;
}

type AxesOpts = {
  w: number;
  h: number;
  padL: number;
  padB: number;
  yt: number[];
  y: (v: number) => number;
  labels: readonly Label[];
  x: (i: number) => number;
  band?: boolean;
  at?: readonly number[] | null;
  tick: Fmt;
  avoid?: [number, number] | null;
};
function axes(g: SVGGElement, el: Element, { w, h, padL, padB, yt, y, labels, x, band = false, at = null, tick, avoid = null }: AxesOpts): void {
  const grid = css(el, '--grid', '#e8e8e8');
  const ink = css(el, '--axis-ink', '#6b6b6b');
  const s = g.ownerSVGElement as SVGSVGElement;
  for (const v of yt) {
    const yy = y(v);
    svg('line', { x1: padL, x2: w, y1: yy, y2: yy, stroke: grid, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }, g);
    text(g, tick(v), { x: padL - 8, y: yy + 4, 'text-anchor': 'end', 'font-size': 12, fill: ink });
  }
  const n = labels.length;
  for (const i of xPicks(labels, w - padL, at)) {
    const lab = String(labels[i]);
    const tw = measure(s, lab, 12);
    const xx = x(i);
    // the first and last labels are pulled inside the plot; none runs past either edge
    let left = band ? xx - tw / 2 : i === 0 ? xx : i === n - 1 ? xx - tw : xx - tw / 2;
    left = Math.max(band ? 0 : padL, Math.min(w - tw, left));
    if (avoid && left < avoid[1] + 8 && left + tw > avoid[0] - 8) continue;
    text(g, lab, { x: left, y: h - padB + 18, 'font-size': 12, fill: ink });
  }
}

export type Annotation = { at?: number; from?: number; to?: number; label: string; short?: string; colour?: string };
export type Reference = { value: number; label?: string; at?: 'left' | 'right' };

type LaneItem = {
  label?: string;
  short?: string;
  colour?: string;
  x?: number;
  x0?: number;
  x1?: number;
  span?: boolean;
  fixed?: number;
  leader?: number;
  glyph?: 'up' | 'down';
  at?: number;
  end?: number;
  drawn?: boolean;
};
type Placed = LaneItem & { text: string; isShort: boolean; row: number; x0: number; x1: number; dropped?: boolean };
type Dropped = LaneItem & { dropped: true };
type Lane = { placed: Placed[]; dropped: Dropped[]; rows: number };
type Notes = Lane & { items: LaneItem[] };

/**
 * Annotation labels sit in rows above the plot, never on the data. A label sits beside the top of
 * its own leader line, right of it or left of it, in one of three rows. A place is free when no other
 * label in that row overlaps it, no leader line passes through it, and its own leader passes through
 * no label below it. Full labels first, then short forms, and only then is a label dropped to its
 * marker's tooltip, with a note in the console.
 */
function lane(s: SVGSVGElement, items: LaneItem[], w: number, maxRows = 3): Lane {
  const gap = 6;
  const rank = (it: LaneItem) => (it.fixed != null ? 0 : it.span ? 1 : 2);
  // fixed captions first, then spans, then markers from left to right
  const order = [...items].sort((a, b) => rank(a) - rank(b) || (a.x ?? a.x0 ?? 0) - (b.x ?? b.x0 ?? 0));
  const opts: Placed[][] = order.map((it) => {
    const out: Placed[] = [];
    for (const str of [it.label, it.short].filter((v): v is string => !!v)) {
      const tw = measure(s, str, 11) + (it.glyph ? 9 : 0);
      const mid = it.span ? ((it.x0 ?? 0) + (it.x1 ?? 0)) / 2 : 0;
      const cands: [number, number][] =
        it.fixed != null
          ? [[it.fixed, it.fixed + tw]]
          : it.span
            ? [
                [mid - tw / 2, mid + tw / 2],
                [it.x0 ?? 0, (it.x0 ?? 0) + tw],
                [(it.x1 ?? 0) - tw, it.x1 ?? 0],
              ]
            : [
                [(it.x ?? 0) + 4, (it.x ?? 0) + 4 + tw],
                [(it.x ?? 0) - 4 - tw, (it.x ?? 0) - 4],
              ];
      for (let row = 0; row < maxRows; row++) for (const [x0, x1] of cands) if (x0 >= 0 && x1 <= w) out.push({ ...it, text: str, isShort: str !== it.label, row, x0, x1 });
    }
    return out;
  });
  const free = (c: Placed, placed: (Placed | Dropped)[]) =>
    placed.every((p) => {
      if (p.dropped) return true;
      const q = p as Placed;
      return (
        !(q.row === c.row && c.x0 < q.x1 + gap && c.x1 > q.x0 - gap) &&
        !(q.leader != null && q.row >= c.row && q.leader > c.x0 - 3 && q.leader < c.x1 + 3) &&
        !(c.leader != null && q.row <= c.row && c.leader > q.x0 - 3 && c.leader < q.x1 + 3)
      );
    });
  let best: (Placed | Dropped)[] | null = null;
  let steps = 0;
  const solve = (i: number, placed: (Placed | Dropped)[], allowShort: boolean, allowDrop: boolean): boolean => {
    if (++steps > 20000) return false;
    if (i === order.length) {
      best = placed.slice();
      return true;
    }
    for (const c of opts[i]) {
      if ((c.isShort && !allowShort) || !free(c, placed)) continue;
      placed.push(c);
      if (solve(i + 1, placed, allowShort, allowDrop)) return true;
      placed.pop();
    }
    if (allowDrop) {
      placed.push({ ...order[i], dropped: true });
      if (solve(i + 1, placed, allowShort, allowDrop)) return true;
      placed.pop();
    }
    return false;
  };
  for (const [sh, dr] of [
    [false, false],
    [true, false],
    [true, true],
  ] as const) {
    steps = 0;
    if (solve(0, [], sh, dr)) break;
  }
  const result: (Placed | Dropped)[] = best ?? order.map((it) => ({ ...it, dropped: true as const }));
  const placed = result.filter((p): p is Placed => !p.dropped);
  const dropped = result.filter((p): p is Dropped => !!p.dropped);
  return { placed, dropped, rows: placed.length ? Math.max(...placed.map((p) => p.row)) + 1 : 0 };
}

function prepNotes(s: SVGSVGElement, annotations: Annotation[] | undefined, x: (i: number) => number, w: number, extra: LaneItem[] = []): Notes {
  const items: LaneItem[] = [];
  for (const a of annotations ?? []) {
    if (a.from != null && a.to != null) items.push({ span: true, x0: Math.min(x(a.from), x(a.to)), x1: Math.max(x(a.from), x(a.to)), label: a.label, short: a.short, colour: a.colour });
    else if (a.at != null) items.push({ x: x(a.at), at: a.at, leader: x(a.at), label: a.label, short: a.short, colour: a.colour });
  }
  items.push(...extra);
  return { items, ...lane(s, items.filter((it) => it.label), w) };
}

// shaded spans go under the data; marker lines and the label rows over it
function notesUnder(g: SVGGElement, el: Element, N: Notes, { padT, h, padB }: { padT: number; h: number; padB: number }): void {
  const ink = css(el, '--axis-ink', '#6b6b6b');
  const op = cssNum(el, '--chart-span', 0.07);
  for (const it of N.items) if (it.span) svg('rect', { x: it.x0, y: padT, width: (it.x1 ?? 0) - (it.x0 ?? 0), height: h - padT - padB, fill: ink, opacity: op }, g);
}

const warned = new WeakMap<Element, Set<string>>();
function notesOver(g: SVGGElement, el: Element, N: Notes, { padT, h, padB }: { padT: number; h: number; padB: number }): void {
  const ink = css(el, '--axis-ink', '#6b6b6b');
  // a marker line runs to the plot's foot, or stops above the bar it points at (it.end)
  for (const it of N.items)
    if (it.leader != null && !it.drawn)
      svg('line', { x1: it.leader, x2: it.leader, y1: padT, y2: Math.max(padT, it.end ?? h - padB), stroke: ink, 'stroke-width': 1, opacity: 0.45, 'shape-rendering': 'crispEdges' }, g);
  for (const p of N.placed) {
    const base = padT - 9 - p.row * ROW;
    if (p.leader != null) svg('line', { x1: p.leader, x2: p.leader, y1: base - 8, y2: padT, stroke: ink, 'stroke-width': 1, opacity: 0.45, 'shape-rendering': 'crispEdges' }, g);
    if (p.glyph) svg('path', { d: p.glyph === 'up' ? `M${p.x0} ${base - 1}l3 -6l3 6z` : `M${p.x0} ${base - 7}l3 6l3 -6z`, fill: ink }, g);
    text(g, p.text, { x: p.x0 + (p.glyph ? 9 : 0), y: base, 'font-size': 11, fill: p.colour || ink });
  }
  const host = (g.ownerSVGElement?.parentNode ?? el) as Element;
  let seen = warned.get(host);
  if (!seen) {
    seen = new Set();
    warned.set(host, seen);
  }
  for (const d of N.dropped) {
    if (d.leader != null) titled(svg('line', { x1: d.leader, x2: d.leader, y1: padT, y2: h - padB, stroke: 'transparent', 'stroke-width': 8 }, g), d.label ?? '');
    if (d.label && !seen.has(d.label)) {
      seen.add(d.label);
      console.warn(`Charts: no room above the plot for "${d.label}". Give it a short form (short) or label fewer markers.`);
    }
  }
}

type Box = { x0: number; x1: number; y0: number; y1: number };
type Pt = [number, number] | null;
// does a label box touch a drawn line? polylines are [[x, y], ...]
function hits(b: Box, lines: Pt[][]): boolean {
  for (const pts of lines)
    for (let k = 1; k < pts.length; k++) {
      const a = pts[k - 1];
      const c = pts[k];
      if (!a || !c) continue;
      if (a[0] === c[0]) {
        if (a[0] >= b.x0 - 3 && a[0] <= b.x1 + 3 && Math.max(a[1], c[1]) >= b.y0 - 3 && Math.min(a[1], c[1]) <= b.y1 + 3) return true;
        continue;
      }
      const lo = Math.max(b.x0 - 3, Math.min(a[0], c[0]));
      const hi = Math.min(b.x1 + 3, Math.max(a[0], c[0]));
      for (let xx = lo; xx <= hi; xx += 2) {
        const yy = a[1] + ((xx - a[0]) / (c[0] - a[0])) * (c[1] - a[1]);
        if (yy >= b.y0 - 3 && yy <= b.y1 + 3) return true;
      }
    }
  return false;
}
const boxHits = (b: Box, boxes: Box[]) => boxes.some((r) => b.x0 < r.x1 + 3 && b.x1 > r.x0 - 3 && b.y0 < r.y1 + 3 && b.y1 > r.y0 - 3);

// a reference line (a budget, an SLO) with its label at an end where no series passes
function references(
  g: SVGGElement,
  el: Element,
  refs: Reference | Reference[] | undefined,
  { y, padL, w, lines = [], boxes = [] }: { y: (v: number) => number; padL: number; w: number; lines?: Pt[][]; boxes?: Box[] },
): void {
  const ink = css(el, '--axis-ink', '#6b6b6b');
  const s = g.ownerSVGElement as SVGSVGElement;
  for (const r of ([] as Reference[]).concat(refs ?? [])) {
    const yy = y(r.value);
    svg('line', { x1: padL, x2: w, y1: yy, y2: yy, stroke: ink, 'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: 0.7 }, g);
    if (!r.label) continue;
    const tw = measure(s, r.label, 11);
    const right = r.at !== 'left';
    const cand = (
      [
        [right, -5],
        [right, 13],
        [!right, -5],
        [!right, 13],
      ] as [boolean, number][]
    ).map(([rt, dy]) => ({ x0: rt ? w - tw : padL + 4, x1: rt ? w : padL + 4 + tw, y0: yy + dy - 9, y1: yy + dy + 2, base: yy + dy }));
    const b = cand.find((c) => !hits(c, lines) && !boxHits(c, boxes)) || cand[0];
    text(g, r.label, { x: b.x0, y: b.base, 'font-size': 11, fill: ink });
  }
}

/** One style object per kind of mark draws both the mark and its legend swatch, so the two cannot drift apart. */
type RectStyle = { fill?: string; hatch?: string; stroke?: string; width?: number; dash?: string; opacity?: number | null; edge?: string };
function paintRect(parent: Element, st: RectStyle, x: number, y: number, w: number, h: number, r = 0): SVGRectElement {
  let fill = st.fill || 'none';
  if (st.hatch) {
    const s = (parent instanceof SVGElement ? parent.ownerSVGElement : null) ?? parent;
    const id = `h${Math.random().toString(36).slice(2, 8)}`;
    const p = svg('pattern', { id, width: 5, height: 5, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, svg('defs', {}, s));
    svg('rect', { width: 5, height: 5, fill: st.fill }, p);
    svg('line', { x1: 0, y1: 0, x2: 0, y2: 5, stroke: st.hatch, 'stroke-width': 1.2 }, p);
    fill = `url(#${id})`;
  }
  const inset = st.stroke ? (st.width || 1) / 2 : 0;
  const n = svg(
    'rect',
    {
      x: x + inset,
      y: y + inset,
      width: Math.max(0, w - inset * 2),
      height: Math.max(0, h - inset * 2),
      rx: r,
      fill,
      stroke: st.stroke,
      'stroke-width': st.stroke ? st.width || 1 : null,
      'stroke-dasharray': st.dash,
      opacity: st.opacity ?? null,
      'shape-rendering': r ? null : 'crispEdges',
    },
    parent,
  );
  if (st.edge) svg('rect', { x, y, width: 3, height: h, rx: Math.min(r, 1.5), fill: st.edge }, parent);
  return n;
}

const legendCss =
  ':where(.chart-legend){list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;line-height:16px;color:var(--muted,#5f6368)}:where(.chart-legend li){display:inline-flex;align-items:center;gap:6px;white-space:nowrap}:where(.chart-legend svg){flex:none;overflow:visible}@media (max-width:480px){:where(.chart-legend){display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}}';
type LegendSpec = { label: string; w?: number; draw: (sw: SVGSVGElement) => void };
/** The legend list. Its swatches read the tokens as they draw, so `repaint` draws them again: the
 *  chart's lifecycle calls it on a theme change, after the chart. */
function legendEl(items: LegendSpec[], target: LegendTarget): { ul: HTMLUListElement; repaint: () => void } {
  if (!document.getElementById('charts-legend-css')) {
    const st = document.createElement('style');
    st.id = 'charts-legend-css';
    st.textContent = legendCss;
    document.head.appendChild(st);
  }
  const ul = document.createElement('ul');
  ul.className = 'chart-legend';
  const swatches: [SVGSVGElement, LegendSpec][] = [];
  for (const it of items) {
    const li = document.createElement('li');
    const sw = svg('svg', { width: it.w || 14, height: 12, 'aria-hidden': 'true' });
    it.draw(sw);
    swatches.push([sw, it]);
    li.append(sw, document.createTextNode(it.label));
    ul.appendChild(li);
  }
  const t = typeof target === 'string' ? document.querySelector(target) : target;
  if (t) t.replaceChildren(ul);
  const repaint = () => {
    for (const [sw, it] of swatches) {
      sw.replaceChildren();
      it.draw(sw);
    }
  };
  return { ul, repaint };
}
const rectSwatch = (st: RectStyle, w = 14, h = 10, r = 2) => (sw: SVGSVGElement) => {
  paintRect(sw, st, 0, (12 - h) / 2, w, h, r);
};

/**
 * Charts are laid out with measured text: draw again once the fonts arrive, and when the box is
 * resized. Their colours are read from the tokens at draw time and baked into the SVG, so they also
 * draw again when the theme changes (lib/theme.ts announces it once the new tokens apply), followed
 * by `onTheme` (the legend's swatches). Returns the cleanup; after it, nothing draws again.
 */
function lifecycle(el: HTMLElement, draw: () => void, onTheme?: () => void): () => void {
  let alive = true;
  const safe = () => {
    if (alive) draw();
  };
  safe();
  const offTheme = onThemeChange(() => {
    safe();
    onTheme?.();
  });
  let ro: ResizeObserver | null = null;
  if (typeof ResizeObserver !== 'undefined') {
    let w = el.clientWidth;
    ro = new ResizeObserver(() => {
      if (Math.abs(el.clientWidth - w) > 4) {
        w = el.clientWidth;
        safe();
      }
    });
    ro.observe(el);
  }
  if (typeof document !== 'undefined' && document.fonts) {
    void document.fonts.ready.then(safe);
    document.fonts.addEventListener?.('loadingdone', safe, { once: true });
  }
  return () => {
    alive = false;
    offTheme();
    ro?.disconnect();
    document.fonts?.removeEventListener?.('loadingdone', safe);
  };
}
const heightOf = (h: number | (() => number)) => (typeof h === 'function' ? h() : h);

// ---------------------------------------------------------------------------------------------
// line

export type LineSeries = { name: string; values: (number | null)[]; colour?: string; dash?: boolean };
export type LineOptions = {
  labels: readonly Label[];
  series: LineSeries[];
  format?: Fmt;
  tickFormat?: Fmt | null;
  height?: number | (() => number);
  fill?: boolean;
  min?: number;
  max?: number;
  step?: boolean;
  annotations?: Annotation[];
  reference?: Reference | Reference[];
  today?: number | null;
  todayLabel?: string;
  endLabel?: boolean;
  yTicks?: number[] | null;
  xAt?: number[] | null;
  legend?: LegendTarget;
};

export function line(
  el: HTMLElement,
  { labels, series, format = fmt.int, tickFormat = null, height = 220, fill = false, min, max, step = false, annotations, reference, today = null, todayLabel = 'Today', endLabel = false, yTicks = null, xAt = null, legend = null }: LineOptions,
): ChartWithLegend {
  const colourOf = (sr: LineSeries, k: number) => sr.colour || (k === 0 ? css(el, '--chart-1', '#2f6fdf') : css(el, `--chart-${k + 1}`, css(el, '--chart-muted', '#a3a3a3')));
  const draw = () => {
    const { s, w, h } = frame(el, heightOf(height));
    const all = series.flatMap((sr) => sr.values.filter((v): v is number => v != null)).concat(([] as Reference[]).concat(reference ?? []).map((r) => r.value));
    if (!all.length) all.push(0);
    // max caps the range (a flat series with one spike): values above it are clipped and their peak labelled
    const top = max ?? Math.max(...all);
    const yt = yTicks || ticks(min ?? Math.min(0, ...all), top, 4);
    const ytTop = yt[yt.length - 1];
    const tick = tickFormat || format;
    const firstVals = series[0]?.values.filter((v): v is number => v != null) ?? [];
    const lastVal = firstVals[firstVals.length - 1];
    const padL = 12 + Math.max(...yt.map((v) => measure(s, tick(v), 12)));
    const padB = 26;
    const padR = endLabel && lastVal != null ? 10 + measure(s, format(lastVal), 12, 600) : 0;
    const x = (i: number) => padL + (i * (w - padL - padR)) / Math.max(1, labels.length - 1);
    // the peaks clipped by max are labelled in the rows above the plot, where the line leaves it
    const peaks: LaneItem[] = [];
    series.forEach((sr, k) =>
      sr.values.forEach((v, i) => {
        const prev = sr.values[i - 1];
        if (v != null && v > ytTop && (i === 0 || prev == null || prev <= ytTop)) {
          let j = i;
          let peak = v;
          while (j < sr.values.length && sr.values[j] != null && (sr.values[j] as number) > ytTop) {
            peak = Math.max(peak, sr.values[j] as number);
            j++;
          }
          peaks.push({ x: x(i), label: `${format(peak)} at ${labels[i]}`, colour: colourOf(sr, k) });
        }
      }),
    );
    const scale = (N: Notes) => {
      const padT = LANE(N.rows);
      return { padT, y: (v: number) => padT + (h - padT - padB) * (1 - (v - yt[0]) / (ytTop - yt[0])) };
    };
    const linesAt = (y: (v: number) => number): Pt[][] =>
      series
        .map((sr) => {
          const out: Pt[] = [];
          sr.values.forEach((v, i) => {
            if (v == null) {
              out.push(null);
              return;
            }
            const p: [number, number] = [x(i), y(Math.min(v, ytTop))];
            const prev = out[out.length - 1];
            if (step && prev) out.push([p[0], prev[1]]);
            out.push(p);
          });
          return out;
        })
        .concat(
          ([] as Reference[]).concat(reference ?? []).map((r) => [
            [padL, y(r.value)],
            [w, y(r.value)],
          ]),
        );
    let N = prepNotes(s, annotations, x, w, peaks);
    let { padT, y } = scale(N);
    let todayBox: (Box & { base: number }) | null = null;
    // the today label goes inside the plot on whichever side of its line no series crosses; failing that, above
    if (today != null && todayLabel) {
      const cut = x(today);
      const tw = measure(s, todayLabel, 11);
      const lns = linesAt(y);
      const cand = (
        [
          [cut + 4, h - padB - 6],
          [cut - 4 - tw, h - padB - 6],
          [cut + 4, padT + 11],
          [cut - 4 - tw, padT + 11],
        ] as [number, number][]
      ).map(([x0, b]) => ({ x0, x1: x0 + tw, y0: b - 9, y1: b + 2, base: b }));
      todayBox = cand.find((b) => b.x0 >= padL && b.x1 <= w && !hits(b, lns)) || null;
      if (!todayBox) {
        N = prepNotes(s, annotations, x, w, [...peaks, { x: cut, leader: cut, drawn: true, label: todayLabel }]);
        ({ padT, y } = scale(N));
      }
    }
    const g = svg('g', {}, s);
    axes(g, el, { w, h, padL, padB, yt, y, labels, x, at: xAt, tick });
    notesUnder(g, el, N, { padT, h, padB });
    series.forEach((sr, k) => {
      const col = colourOf(sr, k);
      const pts: Pt[] = sr.values.map((v, i) => (v == null ? null : [x(i), y(Math.min(v, ytTop))]));
      // step: a balance that only changes on given days holds its value until the next one
      const d = pts.reduce((acc, p, i) => {
        if (!p) return acc;
        const prev = pts[i - 1];
        const head = acc && prev ? (step ? `L${p[0].toFixed(1)} ${prev[1].toFixed(1)}L` : 'L') : 'M';
        return `${acc}${head}${p[0].toFixed(1)} ${p[1].toFixed(1)}`;
      }, '');
      if (fill && k === 0) {
        const first = pts.find((p): p is [number, number] => !!p);
        const last = [...pts].reverse().find((p): p is [number, number] => !!p);
        if (first && last) svg('path', { d: `${d}L${last[0]} ${y(yt[0])}L${first[0]} ${y(yt[0])}Z`, fill: col, opacity: 0.12 }, g);
      }
      if (today == null) svg('path', { d, fill: 'none', stroke: col, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'stroke-dasharray': sr.dash ? '4 3' : null }, g);
      else {
        // a forecast: the line after today is dashed, so a projection never reads as a fact
        const cut = x(today);
        const id = `c${k}${Math.random().toString(36).slice(2, 8)}`;
        const defs = svg('defs', {}, g);
        const a = svg('clipPath', { id: `${id}a` }, defs);
        svg('rect', { x: 0, y: 0, width: cut, height: h }, a);
        const b = svg('clipPath', { id: `${id}b` }, defs);
        svg('rect', { x: cut, y: 0, width: w, height: h }, b);
        svg('path', { d, fill: 'none', stroke: col, 'stroke-width': 2, 'stroke-linejoin': 'round', 'clip-path': `url(#${id}a)` }, g);
        svg('path', { d, fill: 'none', stroke: col, 'stroke-width': 2, 'stroke-dasharray': '5 4', opacity: 0.8, 'clip-path': `url(#${id}b)` }, g);
        if (k === 0) {
          svg('line', { x1: cut, x2: cut, y1: padT, y2: h - padB, stroke: css(el, '--axis-ink', '#6b6b6b'), 'stroke-width': 1, opacity: 0.4 }, g);
          if (todayBox) text(g, todayLabel, { x: todayBox.x0, y: todayBox.base, 'font-size': 11, fill: css(el, '--axis-ink', '#6b6b6b') });
        }
      }
      if (endLabel && k === 0) {
        const li = sr.values.map((v, i) => (v == null ? -1 : i)).filter((i) => i >= 0).pop();
        if (li != null) text(g, format(sr.values[li] as number), { x: x(li) + 6, y: y(Math.min(sr.values[li] as number, ytTop)) + 4, 'font-size': 12, 'font-weight': 600, fill: css(el, '--ink', '#111') });
      }
    });
    references(g, el, reference, { y, padL, w, lines: linesAt(y).slice(0, series.length) });
    notesOver(g, el, N, { padT, h, padB });
    // hover: a rule, a dot per series and a bare tooltip
    const tip = tooltip(el);
    const rule = svg('line', { y1: padT, y2: h - padB, stroke: css(el, '--axis-ink', '#6b6b6b'), 'stroke-width': 1, opacity: 0 }, g);
    const dots = series.map((sr, k) => svg('circle', { r: 3.5, fill: css(el, '--surface', '#fff'), stroke: colourOf(sr, k), 'stroke-width': 2, opacity: 0 }, g));
    const hit = svg('rect', { x: padL, y: padT, width: w - padL, height: h - padT - padB, fill: 'transparent' }, s);
    const move = (ev: PointerEvent) => {
      const r = s.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * w;
      const i = Math.max(0, Math.min(labels.length - 1, Math.round(((px - padL) / (w - padL - padR)) * (labels.length - 1))));
      rule.setAttribute('x1', String(x(i)));
      rule.setAttribute('x2', String(x(i)));
      rule.setAttribute('opacity', '0.35');
      series.forEach((sr, k) => {
        const v = sr.values[i];
        dots[k].setAttribute('cx', String(x(i)));
        dots[k].setAttribute('cy', String(v == null ? -99 : y(Math.min(v, ytTop))));
        dots[k].setAttribute('opacity', v == null ? '0' : '1');
      });
      tip.innerHTML =
        `<div style="opacity:.7">${esc(labels[i])}</div>` +
        series.map((sr) => `<div><b style="font-weight:600">${sr.values[i] == null ? '–' : esc(format(sr.values[i] as number))}</b> ${series.length > 1 ? esc(sr.name) : ''}</div>`).join('');
      const left = Math.min((x(i) / w) * r.width + 12, r.width - tip.offsetWidth - 4);
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = '0px';
      tip.style.opacity = '1';
    };
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', () => {
      tip.style.opacity = '0';
      rule.setAttribute('opacity', '0');
      dots.forEach((dd) => dd.setAttribute('opacity', '0'));
    });
    s.setAttribute('aria-label', `${series.map((sr) => sr.name).join(', ')}, ${labels[0]} to ${labels[labels.length - 1]}; latest ${lastVal == null ? 'no data' : format(lastVal)}`);
  };
  const lg =
    series.length > 1
      ? legendEl(
          series.map((sr, k) => ({
            label: sr.name,
            w: 16,
            draw: (sw: SVGSVGElement) => {
              svg('line', { x1: 0, x2: 16, y1: 6, y2: 6, stroke: colourOf(sr, k), 'stroke-width': 2, 'stroke-dasharray': sr.dash ? '4 3' : null }, sw);
            },
          })),
          legend,
        )
      : null;
  return { legend: lg?.ul ?? null, destroy: lifecycle(el, draw, lg?.repaint) };
}

// ---------------------------------------------------------------------------------------------
// bars

export type BarsOptions = {
  labels: readonly Label[];
  values: number[];
  format?: Fmt;
  tickFormat?: Fmt | null;
  height?: number | (() => number);
  /** one bar (or [indices]) may carry the accent */
  highlight?: number | number[] | null;
  dim?: number[];
  colour?: string;
  mutedColour?: string;
  annotations?: Annotation[];
  reference?: Reference | Reference[];
  xAt?: number[] | null;
};

export function bars(
  el: HTMLElement,
  { labels, values, format = fmt.int, tickFormat = null, height = 200, highlight = null, dim = [], colour, mutedColour, annotations, reference, xAt = null }: BarsOptions,
): ChartHandle {
  const lit = (i: number) => highlight == null || (Array.isArray(highlight) ? highlight.includes(i) : highlight === i);
  const draw = () => {
    const { s, w, h } = frame(el, heightOf(height));
    const yt = ticks(0, Math.max(0, ...values, ...([] as Reference[]).concat(reference ?? []).map((r) => r.value)), 4);
    const ytTop = yt[yt.length - 1];
    const tick = tickFormat || format;
    const padL = 12 + Math.max(...yt.map((v) => measure(s, tick(v), 12)));
    const padB = 26;
    const band = (w - padL) / Math.max(1, values.length);
    const bw = Math.max(2, band * 0.62);
    const x = (i: number) => padL + band * i + band / 2;
    const N = prepNotes(s, annotations, x, w);
    const padT = LANE(N.rows);
    const y = (v: number) => padT + (h - padT - padB) * (1 - v / ytTop);
    const g = svg('g', {}, s);
    axes(g, el, { w, h, padL, padB, yt, y, labels, x, band: true, at: xAt, tick });
    notesUnder(g, el, N, { padT, h, padB });
    for (const it of N.items) if (it.at != null && values[it.at] != null) it.end = y(values[it.at]) - 4;
    const main = colour || css(el, '--chart-1', '#2f6fdf');
    const muted = mutedColour || css(el, '--chart-muted', '#c9c9c9');
    values.forEach((v, i) => {
      const r = svg('rect', { x: x(i) - bw / 2, y: y(v), width: bw, height: Math.max(0, y(0) - y(v)), fill: lit(i) ? main : muted, opacity: dim.includes(i) ? 0.45 : null, 'shape-rendering': 'crispEdges' }, g);
      titled(r, `${labels[i]}: ${format(v)}`);
    });
    references(g, el, reference, { y, padL, w, boxes: values.map((v, i) => ({ x0: x(i) - bw / 2, x1: x(i) + bw / 2, y0: y(v), y1: y(0) })) });
    notesOver(g, el, N, { padT, h, padB });
    s.setAttribute('aria-label', `${labels.length} bars from ${labels[0]} to ${labels[labels.length - 1]}; highest ${format(Math.max(...values))}`);
  };
  return { destroy: lifecycle(el, draw) };
}

// ---------------------------------------------------------------------------------------------
// diverging: two directions on one scale, what went out above the zero line and what came in below

export type DivergingOptions = {
  labels: readonly Label[];
  up?: number[];
  down?: number[];
  values?: number[];
  above?: string;
  below?: string;
  mark?: number[];
  markLabel?: string | null;
  markSide?: 'above' | 'below' | 'both';
  partial?: number | null;
  format?: Fmt;
  tickFormat?: Fmt | null;
  height?: number | (() => number);
  annotations?: Annotation[];
  reference?: Reference | Reference[];
  xAt?: number[] | null;
  legend?: LegendTarget;
};

export function diverging(el: HTMLElement, opts: DivergingOptions): ChartWithSvg {
  const { labels, values, above = 'Above', below = 'Below', mark = [], markLabel = null, markSide = 'below', partial = null, format = fmt.int, tickFormat = null, height = 200, annotations, reference, xAt = null, legend = null } = opts;
  let up = opts.up;
  let down = opts.down;
  if (values) {
    up = values.map((v) => Math.max(0, v || 0));
    down = values.map((v) => Math.max(0, -(v || 0)));
  }
  const upV = up || labels.map(() => 0);
  const downV = down || labels.map(() => 0);
  // read at each draw, so a redraw after a theme change takes the new tokens
  const styles = () => ({ above: { fill: css(el, '--chart-1', '#2f6fdf') }, below: { fill: css(el, '--chart-muted', '#c9c9c9') }, mark: { fill: css(el, '--chart-mark', css(el, '--ink', '#111')) } });
  const marked = (i: number, side: 'above' | 'below') => mark.includes(i) && (markSide === 'both' || markSide === side);
  const tick = (v: number) => (v > 0 ? '+' : v < 0 ? MINUS : '') + (tickFormat || format)(Math.abs(v));
  const draw = () => {
    const st = styles();
    const { s, w, h } = frame(el, heightOf(height));
    const hiUp = Math.max(0, ...upV);
    const hiDown = Math.max(0, ...downV);
    const yt = ticks(-hiDown, hiUp || 1, 4);
    const stp = yt[1] - yt[0];
    if (hiDown > 0 && yt[0] >= -hiDown) yt.unshift(+(yt[0] - stp).toFixed(10));
    const padL = 12 + Math.max(...yt.map((v) => measure(s, tick(v), 12)));
    const padB = 26;
    const band = (w - padL) / Math.max(1, labels.length);
    const bw = Math.max(2, band * 0.62);
    const x = (i: number) => padL + band * i + band / 2;
    // the caption for the upper side opens the label rows above the plot; the lower one sits under the plot, at left
    const N = prepNotes(s, annotations, x, w, [{ fixed: padL, glyph: 'up', label: above }]);
    const padT = LANE(N.rows);
    const y = (v: number) => padT + (h - padT - padB) * (1 - (v - yt[0]) / (yt[yt.length - 1] - yt[0]));
    const g = svg('g', {}, s);
    const ink = css(el, '--axis-ink', '#6b6b6b');
    const bwid = measure(s, below, 11) + 9;
    axes(g, el, { w, h, padL, padB, yt, y, labels, x, band: true, at: xAt, tick, avoid: [padL, padL + bwid] });
    svg('path', { d: `M${padL} ${h - padB + 11}l3 6l3 -6z`, fill: ink }, g);
    text(g, below, { x: padL + 9, y: h - padB + 18, 'font-size': 11, fill: ink });
    notesUnder(g, el, N, { padT, h, padB });
    for (const it of N.items) if (it.at != null) it.end = y(upV[it.at] || 0) - 4;
    labels.forEach((lab, i) => {
      const o = partial === i ? 0.5 : null;
      if (upV[i] > 0) {
        const r = paintRect(g, { ...(marked(i, 'above') ? st.mark : st.above), opacity: o }, x(i) - bw / 2, y(upV[i]), bw, Math.max(0, y(0) - y(upV[i]) - 1));
        titled(r, `${lab}: ${above} ${format(upV[i])}`);
      }
      if (downV[i] > 0) {
        const r = paintRect(g, { ...(marked(i, 'below') ? st.mark : st.below), opacity: o }, x(i) - bw / 2, y(0) + 1, bw, Math.max(0, y(-downV[i]) - y(0) - 1));
        titled(r, `${lab}: ${below} ${format(downV[i])}`);
      }
    });
    svg('line', { x1: padL, x2: w, y1: y(0), y2: y(0), stroke: ink, 'stroke-width': 1, opacity: 0.8, 'shape-rendering': 'crispEdges' }, g);
    references(g, el, reference, { y, padL, w, boxes: labels.map((_, i) => ({ x0: x(i) - bw / 2, x1: x(i) + bw / 2, y0: y(upV[i]), y1: y(-downV[i]) })) });
    notesOver(g, el, N, { padT, h, padB });
    s.setAttribute('aria-label', `${above} above the line and ${below} below it, ${labels[0]} to ${labels[labels.length - 1]}`);
  };
  const items: LegendSpec[] = [];
  if (upV.some((v) => v > 0)) items.push({ label: above, draw: (sw) => rectSwatch(styles().above)(sw) });
  if (downV.some((v) => v > 0)) items.push({ label: below, draw: (sw) => rectSwatch(styles().below)(sw) });
  if (mark.length && markLabel) items.push({ label: markLabel, draw: (sw) => rectSwatch(styles().mark)(sw) });
  const lg = legendEl(items, legend);
  return {
    get svg() {
      return el.querySelector<SVGSVGElement>(':scope > svg');
    },
    legend: lg.ul,
    destroy: lifecycle(el, draw, lg.repaint),
  };
}

// ---------------------------------------------------------------------------------------------
// ring, bar list, plan, bullet

export function ring(el: HTMLElement, { value, label, sub, size = 132, stroke = 8, colour }: { value: number; label: string; sub?: string; size?: number; stroke?: number; colour?: string }): ChartHandle {
  el.innerHTML = '';
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(1, value));
  const wrap = document.createElement('div');
  wrap.style.cssText = `position:relative;width:${size}px;height:${size}px`;
  const s = svg('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img', 'aria-label': `${label}${sub ? ` ${sub}` : ''}` }, wrap);
  // drawn once, so its colours stay token references and follow a theme change by themselves
  svg('circle', { cx: size / 2, cy: size / 2, r, fill: 'none', stroke: 'var(--grid, #e8e8e8)', 'stroke-width': stroke }, s);
  svg('circle', { cx: size / 2, cy: size / 2, r, fill: 'none', stroke: colour || 'var(--chart-1, #2f6fdf)', 'stroke-width': stroke, 'stroke-dasharray': `${c * v} ${c}`, transform: `rotate(-90 ${size / 2} ${size / 2})` }, s);
  const t = document.createElement('div');
  t.style.cssText = 'position:absolute;inset:0;display:grid;place-content:center;text-align:center;font-variant-numeric:tabular-nums';
  t.innerHTML = `<div style="font-size:${Math.round(size / 5.5)}px;font-weight:600;line-height:1">${esc(label)}</div>${sub ? `<div style="font-size:12px;opacity:.7;margin-top:4px">${esc(sub)}</div>` : ''}`;
  wrap.appendChild(t);
  el.appendChild(wrap);
  return { destroy: () => undefined };
}

/** A ranked list. By default the row's fill is the bar (a one-figure list); thin: true puts a 2px
 *  grey bar under each label instead, ink for the top item. */
export function barList(el: HTMLElement, rows: { label: string; value: number }[], { format = fmt.int, colour, thin = false }: { format?: Fmt; colour?: string; thin?: boolean } = {}): ChartHandle {
  el.innerHTML = '';
  const max = Math.max(...rows.map((r) => r.value)) || 1;
  const list = document.createElement('ol');
  list.style.cssText = `list-style:none;margin:0;padding:0;display:grid;gap:${thin ? 10 : 2}px;font-variant-numeric:tabular-nums`;
  const valueCol = Math.max(...rows.map((r) => String(format(r.value)).length)) + 1;
  rows.forEach((r, k) => {
    const li = document.createElement('li');
    li.style.cssText = `display:grid;grid-template-columns:minmax(0,1fr) ${valueCol}ch;align-items:center;column-gap:12px`;
    const a = document.createElement('span');
    a.textContent = r.label;
    a.style.cssText = 'position:relative;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    const b = document.createElement('span');
    b.textContent = format(r.value);
    b.style.cssText = 'font-weight:500;text-align:right';
    if (thin) {
      const bar = document.createElement('span');
      bar.style.cssText = `grid-column:1 / -1;height:2px;margin-top:4px;width:${(r.value / max) * 100}%;background:${k === 0 ? colour || 'var(--ink,#111)' : 'var(--chart-muted,#c9c9c9)'}`;
      li.append(a, b, bar);
    } else {
      const track = document.createElement('span');
      track.style.cssText = 'position:relative;padding:10px 12px;border-radius:6px;isolation:isolate;min-width:0';
      const bar = document.createElement('span');
      bar.style.cssText = `position:absolute;inset:0 auto 0 0;width:${(r.value / max) * 100}%;background:${colour || 'var(--bar-fill, color-mix(in srgb, var(--chart-1, #2f6fdf) 12%, transparent))'};border-radius:inherit;z-index:-1`;
      track.append(bar, a);
      li.append(track, b);
    }
    list.appendChild(li);
  });
  el.appendChild(list);
  return { destroy: () => undefined };
}

export type PlanRow = { label: string; planned: number; spent: number; committed?: number };
/** Planned against spent: the track is the plan, the fill what is spent, a lighter segment what is
 *  committed, and an overrun past the track's end in the negative colour. */
export function plan(
  el: HTMLElement,
  rows: PlanRow[],
  { format = fmt.money('INR', 0), scale = 'row', note = (r: PlanRow) => (r.spent > r.planned ? `${format(r.spent - r.planned)} over` : `${format(r.planned - r.spent - (r.committed || 0))} left`) }: { format?: Fmt; scale?: 'row' | 'plan'; note?: (r: PlanRow) => string } = {},
): ChartHandle {
  el.innerHTML = '';
  const list = document.createElement('ul');
  list.style.cssText = 'list-style:none;margin:0;padding:0;display:grid;gap:14px;font-variant-numeric:tabular-nums';
  const max = Math.max(...rows.map((r) => Math.max(r.planned, r.spent + (r.committed || 0))));
  for (const r of rows) {
    const over = r.spent > r.planned;
    const li = document.createElement('li');
    const top = document.createElement('div');
    top.style.cssText = 'display:flex;justify-content:space-between;gap:12px;margin-bottom:6px';
    top.innerHTML = `<span>${esc(r.label)}</span><span><b style="font-weight:500">${esc(format(r.spent))}</b> <span style="color:var(--muted,#5f6368)">of ${esc(format(r.planned))} · </span><span style="color:${over ? 'var(--negative,#c4242a)' : 'var(--muted,#5f6368)'}">${esc(note(r))}</span></span>`;
    const room = over ? 100 / (1 + Math.max(0.06, (r.spent - r.planned) / r.planned)) : 100;
    const track = document.createElement('div');
    track.style.cssText = `position:relative;height:8px;border-radius:999px;background:var(--rule,#eceeef);overflow:visible;width:${scale === 'plan' ? (r.planned / max) * room : room}%`;
    const pct = (v: number) => (v / r.planned) * 100;
    const spent = Math.min(r.spent, r.planned);
    track.innerHTML =
      `<span style="position:absolute;inset:0 auto 0 0;width:${pct(spent)}%;border-radius:999px;background:var(--chart-1,#2f6fdf)"></span>` +
      (r.committed ? `<span style="position:absolute;top:0;bottom:0;left:${pct(spent)}%;width:${Math.min(pct(r.committed), 100 - pct(spent))}%;background:var(--chart-1,#2f6fdf);opacity:.35"></span>` : '') +
      (over
        ? `<span style="position:absolute;top:-3px;bottom:-3px;left:100%;width:1px;background:var(--text,#111)"></span><span style="position:absolute;top:0;bottom:0;left:calc(100% + 2px);width:max(12px, ${pct(r.spent - r.planned)}%);border-radius:0 999px 999px 0;background:var(--negative,#c4242a)"></span>`
        : '');
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', `${r.label}: ${format(r.spent)} of ${format(r.planned)}${r.committed ? `, ${format(r.committed)} committed` : ''}${over ? ', over plan' : ''}`);
    li.append(top, track);
    list.appendChild(li);
  }
  el.appendChild(list);
  return { destroy: () => undefined };
}

export type BulletRow = { label: string; value: number; target: number; tolerance?: number };
/** Share against a target: the current share as a bar and a 1px tick at the target. */
export function bullet(el: HTMLElement, rows: BulletRow[], { format = fmt.pct(1), max = Math.max(...rows.map((r) => Math.max(r.value, r.target))) * 1.15 }: { format?: Fmt; max?: number } = {}): ChartHandle {
  el.innerHTML = '';
  const list = document.createElement('ul');
  list.style.cssText = 'list-style:none;margin:0;padding:0;display:grid;gap:12px;font-variant-numeric:tabular-nums';
  for (const r of rows) {
    const drift = r.value - r.target;
    const li = document.createElement('li');
    li.style.cssText = 'display:grid;grid-template-columns:minmax(90px,30%) 1fr auto;align-items:center;gap:12px';
    li.innerHTML = `<span>${esc(r.label)}</span><span style="position:relative;height:10px;background:var(--rule,#eceeef);border-radius:2px"><span style="position:absolute;inset:0 auto 0 0;width:${(r.value / max) * 100}%;background:var(--chart-1,#2f6fdf);border-radius:2px"></span><span style="position:absolute;top:-4px;bottom:-4px;left:${(r.target / max) * 100}%;width:2px;margin-left:-1px;background:var(--text,#111)"></span></span><span><b style="font-weight:500">${esc(format(r.value))}</b> <span style="color:${Math.abs(drift) >= (r.tolerance ?? 0.02) ? 'var(--negative,#c4242a)' : 'var(--muted,#5f6368)'}">${drift >= 0 ? '+' : MINUS}${esc(format(Math.abs(drift)))} vs ${esc(format(r.target))}</span></span>`;
    li.setAttribute('aria-label', `${r.label}: ${format(r.value)}, target ${format(r.target)}`);
    list.appendChild(li);
  }
  el.appendChild(list);
  return { destroy: () => undefined };
}

// ---------------------------------------------------------------------------------------------
// row charts (timeline, matrix, time strip): a label column with an optional grey subline

type RowLabel = { label: string; sub?: string };
function rowLabel(g: SVGGElement, s: SVGSVGElement, el: Element, r: RowLabel, { y0, pitch, lw }: { y0: number; pitch: number; lw: number }): void {
  const ink = css(el, '--ink', '#111');
  const muted = css(el, '--axis-ink', '#6b6b6b');
  if (r.sub) {
    text(g, fit(s, r.label, lw - 12, 14), { x: 0, y: y0 + pitch / 2 - 3, 'font-size': 14, fill: ink });
    text(g, fit(s, r.sub, lw - 12, 12), { x: 0, y: y0 + pitch / 2 + 13, 'font-size': 12, fill: muted });
  } else text(g, fit(s, r.label, lw - 12, 14), { x: 0, y: y0 + pitch / 2 + 5, 'font-size': 14, fill: ink });
}

export type DayLabel = string | { label: string; short?: string };
const dayText = (d: DayLabel | undefined): string => (typeof d === 'object' && d ? d.label : String(d ?? ''));

// day headers: the full label when every one fits its column, else the short form, else every k-th
// short label with today always shown and its neighbours left out, so no two headers ever touch
function dayHeads(g: SVGGElement, s: SVGSVGElement, el: Element, days: DayLabel[], { x0, cw, y, today }: { x0: number; cw: number; y: number; today: number | null }): void {
  const ink = css(el, '--ink', '#111');
  const muted = css(el, '--axis-ink', '#6b6b6b');
  const full = days.map(dayText);
  const short = days.map((d) => (typeof d === 'object' && d ? d.short || dayText(d) : dayText(d).split(' ').pop() ?? ''));
  const wide = (labs: string[]) => Math.max(...labs.map((t) => measure(s, t, 12))) + 6;
  const labs = wide(full) <= cw ? full : short;
  const k = Math.max(1, Math.ceil(wide(labs) / cw));
  labs.forEach((t, i) => {
    if (i !== today && (i % k || (today != null && k > 1 && Math.abs(i - today) < k))) return;
    text(g, t, { x: x0 + i * cw + cw / 2, y, 'text-anchor': 'middle', 'font-size': 12, 'font-weight': i === today ? 600 : 400, fill: i === today ? ink : muted });
  });
}

export type BookingKind = { label: string; mark?: 'fill' | 'hatch' | 'step' | 'outline' | 'hue'; code?: string; tone?: 'negative'; dash?: boolean; colour?: string };
// The style of each kind of booking: a pale fill, a 1px diagonal hatch, a darker grey step, an
// outline, or the one chart hue for the screen's main data element.
function kindStyle(el: Element, k: Partial<BookingKind> = {}): RectStyle {
  const ink = css(el, '--ink', '#111');
  const surface = css(el, '--surface', '#fff');
  const neg = css(el, '--negative', '#c4242a');
  const hue = k.colour || css(el, '--chart-1', '#2f6fdf');
  const tone = k.tone === 'negative' ? { stroke: neg, width: 1.5 } : {};
  const dash = k.dash ? { dash: '3 2' } : {};
  if (k.mark === 'hatch') return { fill: mix(el, ink, surface, 0.04), hatch: mix(el, ink, surface, 0.32), ...tone, ...dash };
  if (k.mark === 'step') return { fill: mix(el, ink, surface, 0.24), ...tone, ...dash };
  if (k.mark === 'outline') return { fill: surface, stroke: mix(el, ink, surface, 0.55), width: 1, ...tone, ...dash };
  if (k.mark === 'hue') return { fill: mix(el, hue, surface, 0.18), edge: hue, ...tone, ...dash };
  return { fill: mix(el, ink, surface, 0.12), ...tone, ...dash };
}

export type TimelineBar = { from: number; to: number; kind: string; label?: string; code?: string; note?: string };
export type TimelineRow = { label: string; sub?: string; bars?: TimelineBar[]; group?: undefined } | { group: string; label?: undefined; sub?: undefined; bars?: undefined };

/** Who is away when: rows down the left, days across, a bar per booking told apart by its kind. */
export function timeline(
  el: HTMLElement,
  rows: TimelineRow[],
  kinds: Record<string, BookingKind> = {},
  {
    days = [],
    weekends = [],
    today = null,
    holidays = [],
    labelWidth = null,
    rowHeight = null,
    barHeight = 22,
    onSelect = null,
    legend = null,
    extras = true,
  }: {
    days?: DayLabel[];
    weekends?: number[];
    today?: number | null;
    holidays?: { day: number; label: string }[];
    labelWidth?: number | null;
    rowHeight?: number | null;
    barHeight?: number;
    onSelect?: ((bar: TimelineBar, row: TimelineRow) => void) | null;
    legend?: LegendTarget;
    extras?: boolean;
  } = {},
): ChartWithSvg {
  const draw = () => {
    const pitch = rowHeight || (rows.some((r) => r.sub) ? 44 : 32);
    const head = 30;
    const groupH = 30;
    const height = head + rows.reduce((a, r) => a + (r.group ? groupH : pitch), 0) + 2;
    const { s, w, h } = frame(el, height);
    const lw = labelWidth || Math.round(Math.min(220, Math.max(w < 480 ? 96 : 120, w * 0.2)));
    const cw = (w - lw) / Math.max(1, days.length);
    const col = (i: number) => lw + i * cw;
    const ink = css(el, '--ink', '#111');
    const muted = css(el, '--axis-ink', '#6b6b6b');
    const rule = css(el, '--rule', '#f0f0f0');
    const raised = css(el, '--raised', '#f4f5f6');
    const hue = css(el, '--chart-1', '#2f6fdf');
    const g = svg('g', {}, s);
    for (const i of weekends) svg('rect', { x: col(i), y: head, width: cw, height: h - head, fill: raised }, g);
    for (let i = 0; i <= days.length; i++) svg('line', { x1: col(i), x2: col(i), y1: head, y2: h, stroke: rule, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }, g);
    dayHeads(g, s, el, days, { x0: lw, cw, y: head - 10, today });
    for (const hd of holidays) titled(svg('circle', { cx: col(hd.day) + cw / 2, cy: head - 3, r: 2, fill: muted }, g), hd.label);
    if (today != null) svg('line', { x1: col(today) + cw / 2, x2: col(today) + cw / 2, y1: head, y2: h, stroke: hue, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }, g);
    let y0 = head;
    for (const r of rows) {
      if (r.group) {
        svg('line', { x1: 0, x2: w, y1: y0 + 0.5, y2: y0 + 0.5, stroke: rule, 'stroke-width': 1 }, g);
        text(g, r.group, { x: 0, y: y0 + 20, 'font-size': 12, 'font-weight': 500, fill: muted });
        y0 += groupH;
        continue;
      }
      rowLabel(g, s, el, { label: r.label as string, sub: r.sub }, { y0, pitch, lw });
      const bs = (r.bars || []).filter((b) => b.to >= 0 && b.from < days.length).sort((a, b) => a.from - b.from);
      bs.forEach((b, k) => {
        const kind = kinds[b.kind] || ({} as Partial<BookingKind>);
        const st = kindStyle(el, kind);
        const x0 = col(Math.max(0, b.from)) + 2;
        const x1 = col(Math.min(days.length - 1, b.to) + 1) - 2;
        const by = y0 + (pitch - barHeight) / 2;
        const grp = svg('g', onSelect ? { tabindex: 0, role: 'button', style: 'cursor:pointer' } : {}, g);
        paintRect(grp, st, x0, by, x1 - x0, barHeight, 4);
        const label = b.label ?? kind.label ?? '';
        const code = b.code ?? kind.code;
        const tw = measure(s, label, 12);
        const labInk = kind.tone === 'negative' ? css(el, '--negative', '#c4242a') : ink;
        const next = bs[k + 1] ? col(Math.max(0, bs[k + 1].from)) : w;
        // over a hatch the label keeps a halo of the bar's own fill, so the lines never run through the words
        const halo: Attrs = st.hatch ? { stroke: st.fill, 'stroke-width': 4, 'stroke-linejoin': 'round', 'paint-order': 'stroke' } : {};
        if (label && tw + 16 <= x1 - x0) text(grp, label, { x: x0 + 8 + (st.edge ? 2 : 0), y: by + barHeight / 2 + 4, 'font-size': 12, fill: labInk, ...halo });
        else if (code && measure(s, code, 12) + 8 <= x1 - x0) text(grp, code, { x: (x0 + x1) / 2, y: by + barHeight / 2 + 4, 'text-anchor': 'middle', 'font-size': 12, fill: labInk, ...halo });
        else if (label && x1 + 6 + tw <= Math.min(next - 6, w)) text(grp, label, { x: x1 + 6, y: by + barHeight / 2 + 4, 'font-size': 12, fill: kind.tone === 'negative' ? labInk : muted });
        titled(grp, `${r.label}: ${label}${b.note ? ` · ${b.note}` : ''}`);
        if (onSelect) {
          grp.addEventListener('click', () => onSelect(b, r));
          grp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onSelect(b, r);
            }
          });
        }
      });
      y0 += pitch;
    }
    s.setAttribute('aria-label', `${rows.filter((r) => !r.group).length} rows across ${days.length} days, ${dayText(days[0])} to ${dayText(days[days.length - 1])}`);
  };
  const used = new Set(rows.flatMap((r) => (r.bars || []).map((b) => b.kind)));
  const items: LegendSpec[] = Object.entries(kinds)
    .filter(([id]) => used.has(id))
    .map(([, k]) => ({
      label: k.label,
      w: 22,
      draw: (sw: SVGSVGElement) => {
        paintRect(sw, kindStyle(el, k), 0, 0, 22, 12, 3);
      },
    }));
  if (extras && today != null)
    items.push({
      label: 'Today',
      w: 8,
      draw: (sw) => {
        svg('line', { x1: 4, x2: 4, y1: 0, y2: 12, stroke: css(el, '--chart-1', '#2f6fdf'), 'stroke-width': 1.5 }, sw);
      },
    });
  const lg = legendEl(items, legend);
  return {
    get svg() {
      return el.querySelector<SVGSVGElement>(':scope > svg');
    },
    legend: lg.ul,
    destroy: lifecycle(el, draw, lg.repaint),
  };
}

export type MatrixRow = { label: string; sub?: string; values: (number | null)[]; titles?: string[] };
/** Is there enough cover? A team-by-day grid of slack against the minimum (+2, +1, 0, −1). */
export function matrix(
  el: HTMLElement,
  rows: MatrixRow[],
  {
    cols = [],
    comfort = 2,
    today = null,
    format = fmt.signed(),
    labelWidth = null,
    rowHeight = null,
    levels = ['Room to spare', 'Tight', 'At the minimum', 'Short'],
    legend = null,
  }: { cols?: DayLabel[]; comfort?: number; today?: number | null; format?: Fmt; labelWidth?: number | null; rowHeight?: number | null; levels?: string[]; legend?: LegendTarget } = {},
): ChartWithSvg {
  const level = (v: number) => (v >= comfort ? 0 : v > 0 ? 1 : v === 0 ? 2 : 3);
  const styles = () => {
    const neg = css(el, '--negative', '#c4242a');
    const surface = css(el, '--surface', '#fff');
    const ink = css(el, '--ink', '#111');
    return [
      { fill: surface, ink: css(el, '--axis-ink', '#6b6b6b'), stroke: css(el, '--grid', '#e5e5e5') },
      { fill: mix(el, neg, surface, 0.1), ink },
      { fill: mix(el, neg, surface, 0.26), ink },
      { fill: neg, ink: surface },
    ];
  };
  const draw = () => {
    const pitch = rowHeight || (rows.some((r) => r.sub) ? 44 : 32);
    const head = 26;
    const { s, w } = frame(el, head + rows.length * pitch);
    const lw = labelWidth || Math.round(Math.min(220, Math.max(w < 480 ? 96 : 120, w * 0.22)));
    const cw = (w - lw) / Math.max(1, cols.length);
    const ST = styles();
    const muted = css(el, '--axis-ink', '#6b6b6b');
    const g = svg('g', {}, s);
    dayHeads(g, s, el, cols, { x0: lw, cw, y: head - 10, today });
    rows.forEach((r, k) => {
      const y0 = head + k * pitch;
      rowLabel(g, s, el, r, { y0, pitch, lw });
      const ch = Math.min(pitch - 6, 28);
      const cy = y0 + (pitch - ch) / 2;
      r.values.forEach((v, i) => {
        const cx = lw + i * cw + 1;
        if (v == null) {
          svg('rect', { x: cx + cw / 2 - 4, y: cy + ch / 2 - 1, width: 6, height: 2, fill: muted }, g);
          return;
        }
        const st = ST[level(v)];
        const cell = paintRect(g, { fill: st.fill }, cx, cy, cw - 2, ch, 3);
        text(g, format(v), { x: cx + (cw - 2) / 2, y: cy + ch / 2 + 4, 'text-anchor': 'middle', 'font-size': 12, 'font-weight': level(v) === 3 ? 600 : 400, fill: st.ink });
        titled(cell, `${r.label}, ${dayText(cols[i])}: ${r.titles?.[i] || `${format(v)} against the minimum`}`);
      });
    });
    s.setAttribute('aria-label', `Cover against the minimum for ${rows.length} rows across ${cols.length} days`);
  };
  const seen = new Set<number>(rows.flatMap((r) => r.values.filter((v): v is number => v != null).map(level)));
  const items: LegendSpec[] = [0, 1, 2, 3]
    .filter((l) => seen.has(l))
    .map((l) => ({
      label: levels[l],
      w: 14,
      draw: (sw: SVGSVGElement) => {
        const st = styles()[l];
        rectSwatch(l === 0 ? { fill: st.fill, stroke: (st as { stroke?: string }).stroke } : { fill: st.fill }, 14, 12, 3)(sw);
      },
    }));
  const lg = legendEl(items, legend);
  return {
    get svg() {
      return el.querySelector<SVGSVGElement>(':scope > svg');
    },
    legend: lg.ul,
    destroy: lifecycle(el, draw, lg.repaint),
  };
}

export type TimeStripRow = { label: string; sub?: string; at?: string | number | null; compare?: string | number | null; value?: string; note?: string };
/** When, within the day: one row per item on a shared open-close axis; an ink dot at today's time
 *  and a hollow tick at the comparison day's. */
export function timeStrip(
  el: HTMLElement,
  rows: TimeStripRow[],
  {
    open = '08:00',
    close = '18:00',
    step = 60,
    todayLabel = 'Today',
    compareLabel = null,
    labelWidth = null,
    valueWidth = null,
    rowHeight = null,
    legend = null,
  }: { open?: string | number; close?: string | number; step?: number; todayLabel?: string; compareLabel?: string | null; labelWidth?: number | null; valueWidth?: number | null; rowHeight?: number | null; legend?: LegendTarget } = {},
): ChartWithSvg {
  const o = minutes(open) as number;
  const c = minutes(close) as number;
  const st = { today: { r: 4.5 }, compare: { w: 5, h: 12 } };
  const dot = (parent: Element, cx: number, cy: number) => svg('circle', { cx, cy, r: st.today.r, fill: css(el, '--ink', '#111') }, parent);
  const tick = (parent: Element, cx: number, cy: number) =>
    svg('rect', { x: cx - st.compare.w / 2, y: cy - st.compare.h / 2, width: st.compare.w, height: st.compare.h, rx: 2.5, fill: css(el, '--surface', '#fff'), stroke: css(el, '--axis-ink', '#6b6b6b'), 'stroke-width': 1.5 }, parent);
  const draw = () => {
    const narrow = (el.clientWidth || 600) < 560;
    const axisH = 24;
    const pitch = rowHeight || (narrow ? 62 : rows.some((r) => r.sub || r.note) ? 44 : 32);
    const { s, w } = frame(el, rows.length * pitch + axisH);
    const lw = narrow ? 0 : labelWidth || Math.round(Math.min(200, Math.max(110, w * 0.26)));
    const vw = narrow
      ? 0
      : valueWidth ?? Math.min(w * 0.32, Math.max(0, ...rows.map((r) => Math.max(r.value ? measure(s, r.value, 13, 500) : 0, r.note ? measure(s, r.note, 12) : 0))) + (rows.some((r) => r.value || r.note) ? 16 : 0));
    const x0 = narrow ? 18 : lw + 6;
    const x1 = narrow ? w - 18 : w - vw - 6;
    const tx = (m: number) => x0 + ((m - o) / Math.max(1, c - o)) * (x1 - x0);
    const ink = css(el, '--ink', '#111');
    const muted = css(el, '--axis-ink', '#6b6b6b');
    const rule = css(el, '--rule', '#f0f0f0');
    const track = css(el, '--chart-track', mix(el, ink, css(el, '--surface', '#fff'), 0.16));
    const g = svg('g', {}, s);
    const plotH = rows.length * pitch;
    // the axis: one step from the opening, the labels thinned by the same rule as the charts
    const marks: number[] = [];
    for (let m = Math.ceil(o / step) * step; m <= c; m += step) marks.push(m);
    const picks = xPicks(marks.map(fmt.clock), x1 - x0);
    for (const i of picks) {
      const xx = tx(marks[i]);
      if (!narrow) svg('line', { x1: xx, x2: xx, y1: 0, y2: plotH, stroke: rule, 'stroke-width': 1, 'shape-rendering': 'crispEdges' }, g);
      const lab = fmt.clock(marks[i]);
      const tw = measure(s, lab, 12);
      text(g, lab, { x: Math.max(x0 - 2, Math.min(x1 + 6 - tw, xx - tw / 2)), y: plotH + 17, 'font-size': 12, fill: muted });
    }
    rows.forEach((r, k) => {
      const y0 = k * pitch;
      const cy = narrow ? y0 + pitch - 12 : y0 + pitch / 2;
      if (narrow) {
        const vwid = r.value ? measure(s, r.value, 13, 500) + 12 : 0;
        text(g, fit(s, r.label, w - vwid, 14), { x: 0, y: y0 + 16, 'font-size': 14, fill: ink });
        if (r.value) text(g, r.value, { x: w, y: y0 + 16, 'text-anchor': 'end', 'font-size': 13, 'font-weight': 500, fill: ink });
        const second = [r.sub, r.note].filter(Boolean).join(' · ');
        if (second) text(g, fit(s, second, w, 12), { x: 0, y: y0 + 33, 'font-size': 12, fill: muted });
      } else rowLabel(g, s, el, r, { y0, pitch, lw });
      svg('line', { x1: x0, x2: x1, y1: cy, y2: cy, stroke: track, 'stroke-width': 2, 'stroke-linecap': 'round' }, g);
      const cm = minutes(r.compare);
      const am = minutes(r.at);
      if (cm != null) titled(tick(g, tx(cm), cy), `${compareLabel || 'Comparison'}: ${fmt.clock(cm)}`);
      if (am != null) titled(dot(g, tx(am), cy), `${todayLabel}: ${fmt.clock(am)}`);
      if (narrow) return;
      if (r.note) {
        if (r.value) text(g, r.value, { x: w, y: cy - 3, 'text-anchor': 'end', 'font-size': 13, 'font-weight': 500, fill: ink });
        text(g, fit(s, r.note, vw - 8, 12), { x: w, y: r.value ? cy + 13 : cy + 4, 'text-anchor': 'end', 'font-size': 12, fill: muted });
      } else if (r.value) text(g, r.value, { x: w, y: cy + 5, 'text-anchor': 'end', 'font-size': 13, 'font-weight': 500, fill: ink });
    });
    s.setAttribute('aria-label', `${rows.length} rows between ${fmt.clock(o)} and ${fmt.clock(c)}`);
  };
  const items: LegendSpec[] = [];
  if (rows.some((r) => minutes(r.at) != null))
    items.push({
      label: todayLabel,
      w: 10,
      draw: (sw) => {
        dot(sw, 5, 6);
      },
    });
  if (compareLabel && rows.some((r) => minutes(r.compare) != null))
    items.push({
      label: compareLabel,
      w: 8,
      draw: (sw) => {
        tick(sw, 4, 6);
      },
    });
  const lg = legendEl(items, legend);
  return {
    get svg() {
      return el.querySelector<SVGSVGElement>(':scope > svg');
    },
    legend: lg.ul,
    destroy: lifecycle(el, draw, lg.repaint),
  };
}

/** The same object the original script put on window. */
export const Charts = { line, bars, diverging, ring, barList, plan, bullet, timeline, matrix, timeStrip, ticks, fmt };
