'use client';

import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';

import { readGeoDiurnal, type FiresResponse, type GeostationaryDiurnalResponse, type PassesResponse } from '@/lib/api';
import { duration, number, startOfIstDay, timeIST, toDate, type DateInput } from '@/lib/format';
import { FIRE_INSTRUMENTS, type PassWindow } from '@/lib/orbits';

/**
 * The pass strip: 24 hours of one IST day with the fire satellites' observation windows lit, the
 * blind hours dark, the day's FIRMS detections as ticks, an optional geostationary hourly row, and
 * "now" marked. Mission control's signature element (.taste-skill/notes.md).
 *
 *   <PassStrip now={now} windows={windowsFromPasses(passes.data)} detections={detectionsFromFires(fires.data)}
 *              geostationary={geoHourly(diurnal.data)} />
 *
 * All inputs are data from the API (or computed from its orbital elements); the component draws
 * nothing it was not given. With no windows it still draws the day, all dark, which is honest.
 * Colours are CSS tokens set through `style` (SVG presentation attributes do not reliably resolve
 * var() in every browser).
 */

export type StripWindow = { start: DateInput; end: DateInput; tca?: DateInput; satellite: string; instrument?: string };
export type StripDetection = { t: DateInput; count?: number };
export type StripHourly = { hour_ist: number; value: number };

export type PassStripProps = {
  windows: readonly StripWindow[];
  detections?: readonly StripDetection[];
  geostationary?: readonly StripHourly[] | null;
  /** Caption of the geostationary row (default "Geostationary GK-2A, fire pixels per hour"). */
  geoLabel?: string;
  /** How a geostationary value is read in its tooltip (default "fire pixels"). */
  geoUnit?: string;
  /** Now: marked with a line. Null (server render, before mount) leaves it out. */
  now?: Date | null;
  /** Any instant in the IST day to show (default: the day of `now`). */
  day?: DateInput;
  /** IST hour the strip starts at (default 0: midnight to midnight). */
  startHour?: number;
  className?: string;
  style?: CSSProperties;
};

// ---------------------------------------------------------------------------------------------
// adapters from API shapes

/** Windows from /api/orbits/passes (fire instruments only by default). */
export function windowsFromPasses(r: PassesResponse | null | undefined, instruments: readonly string[] = FIRE_INSTRUMENTS): StripWindow[] {
  return (r?.passes ?? [])
    .filter((p) => instruments.includes((p.instrument ?? '').toUpperCase()))
    .map((p) => ({ start: p.start_utc, end: p.end_utc, tca: p.peak_utc, satellite: p.satellite, instrument: p.instrument }));
}

/** Windows from lib/orbits.ts predictions (the same criterion, computed in the browser). */
export function windowsFromOrbits(passes: readonly PassWindow[], instruments: readonly string[] = FIRE_INSTRUMENTS): StripWindow[] {
  return passes.filter((p) => instruments.includes(p.instrument)).map((p) => ({ start: p.start, end: p.end, tca: p.tca, satellite: p.satName, instrument: p.instrument }));
}

/** One tick per detection time from /api/fires. */
export function detectionsFromFires(r: FiresResponse | null | undefined): StripDetection[] {
  return (r?.features?.features ?? []).map((f) => ({ t: f.properties.acq_utc }));
}

/** GK-2A fire pixels per IST hour from /api/geostationary/diurnal. */
export function geoHourly(r: GeostationaryDiurnalResponse | null | undefined): StripHourly[] {
  return readGeoDiurnal(r).map((b) => ({ hour_ist: b.hour_ist, value: b.pixels }));
}

// ---------------------------------------------------------------------------------------------
// layout

const DAY_MS = 86_400_000;
const STRIP_H = 44;
const GEO_H = 30;
const LABEL_H = 18; // a row's caption above it
const AXIS_H = 22;
const TICK_BIN_MS = 10 * 60_000;
const charW = (s: string, size: number) => s.length * size * 0.56; // Work Sans average advance

// The app tokens, with page-surface fallbacks so the strip also works outside [data-surface="app"].
const FALLBACK: Record<string, string> = {
  '--axis-ink': 'var(--muted)',
  '--raised': 'var(--surface)',
  '--chart-window': 'rgb(244 239 232 / 0.16)',
  '--chart-1': '#f4efe8',
  '--chart-2': 'var(--mark-fire)',
  '--grid': '#2b2620',
};
const tokenOf = (token: string) => `var(${token}${FALLBACK[token] ? `, ${FALLBACK[token]}` : ''})`;
const fillOf = (token: string): CSSProperties => ({ fill: tokenOf(token) });
const strokeOf = (token: string): CSSProperties => ({ stroke: tokenOf(token) });
const INK: CSSProperties = { fill: 'var(--text, var(--ink))' };

type Cluster = { start: number; end: number; passes: { start: number; end: number; tca: number; satellite: string }[]; instruments: Set<string> };

function cluster(windows: readonly StripWindow[], from: number, to: number): Cluster[] {
  const items = windows
    .map((w) => ({ start: toDate(w.start)?.getTime() ?? NaN, end: toDate(w.end)?.getTime() ?? NaN, tca: toDate(w.tca)?.getTime() ?? NaN, satellite: w.satellite, instrument: (w.instrument ?? '').toUpperCase() }))
    .filter((w) => Number.isFinite(w.start) && Number.isFinite(w.end) && w.end > from && w.start < to)
    .sort((a, b) => a.start - b.start);
  const out: Cluster[] = [];
  for (const w of items) {
    const last = out[out.length - 1];
    const tca = Number.isFinite(w.tca) ? w.tca : (w.start + w.end) / 2;
    // passes less than an hour apart read as one window ("VIIRS 12:58-14:43")
    if (last && w.start - last.end < 60 * 60_000) {
      last.end = Math.max(last.end, w.end);
      last.passes.push({ start: w.start, end: w.end, tca, satellite: w.satellite });
      if (w.instrument) last.instruments.add(w.instrument);
    } else out.push({ start: w.start, end: w.end, passes: [{ start: w.start, end: w.end, tca, satellite: w.satellite }], instruments: new Set(w.instrument ? [w.instrument] : []) });
  }
  return out;
}

export function PassStrip({ windows, detections = [], geostationary = null, geoLabel, geoUnit = 'fire pixels', now = null, day, startHour = 0, className, style }: PassStripProps) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.round(el.clientWidth)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const hasGeo = !!geostationary?.length;
  const height = LABEL_H + STRIP_H + (hasGeo ? LABEL_H + GEO_H + 6 : 0) + AXIS_H;

  const model = useMemo(() => {
    const anchor = toDate(day) ?? now;
    const dayStart = anchor ? (startOfIstDay(anchor)?.getTime() ?? null) : null;
    if (dayStart == null) return null;
    const from = dayStart + startHour * 3_600_000;
    const to = from + DAY_MS;
    const clusters = cluster(windows, from, to);
    // detections in 10-minute bins
    const bins = new Map<number, number>();
    for (const d of detections) {
      const t = toDate(d.t)?.getTime();
      if (t == null || t < from || t >= to) continue;
      const k = Math.floor((t - from) / TICK_BIN_MS);
      bins.set(k, (bins.get(k) ?? 0) + (d.count ?? 1));
    }
    const total = [...bins.values()].reduce((a, b) => a + b, 0);
    // blind spans: the gaps between lit clusters inside the day
    const edges = [from, ...clusters.flatMap((c) => [Math.max(from, c.start), Math.min(to, c.end)]), to];
    const gaps: { start: number; end: number }[] = [];
    for (let i = 0; i + 1 < edges.length; i += 2) if (edges[i + 1] - edges[i] > 0) gaps.push({ start: edges[i], end: edges[i + 1] });
    return { from, to, clusters, bins, total, gaps };
  }, [windows, detections, day, now, startHour]);

  const label = useMemo(() => {
    if (!model) return 'Satellite pass strip';
    const looks = model.clusters.map((c) => `${[...c.instruments].join(' and ') || 'satellite'} ${timeIST(c.start)} to ${timeIST(c.end)}`).join('; ');
    const longest = model.gaps.reduce<{ start: number; end: number } | null>((a, g) => (!a || g.end - g.start > a.end - a.start ? g : a), null);
    return `Fire satellites looked at the site ${looks || 'never'} this day. ${number(model.total)} detections. ${longest ? `Longest blind span ${timeIST(longest.start)} to ${timeIST(longest.end)}, ${duration(longest.end - longest.start)}.` : ''}`;
  }, [model]);

  const W = width;
  const x = (t: number) => (model ? ((t - model.from) / DAY_MS) * W : 0);
  const rowTop = LABEL_H;
  const geoTop = rowTop + STRIP_H + 6 + LABEL_H;
  const axisTop = (hasGeo ? geoTop + GEO_H : rowTop + STRIP_H) + 4;
  const maxBin = model ? Math.max(1, ...model.bins.values()) : 1;
  const maxGeo = hasGeo ? Math.max(1, ...(geostationary ?? []).map((g) => g.value)) : 1;
  const hourLabels = model ? Array.from({ length: 25 }, (_, i) => (startHour + i) % 24) : [];
  const step = W >= 720 ? 3 : 6;

  return (
    <div ref={box} className={className} style={{ position: 'relative', width: '100%', height, ...style }}>
      {model && W > 0 ? (
        <svg width={W} height={height} viewBox={`0 0 ${W} ${height}`} role="img" aria-label={label} style={{ display: 'block', overflow: 'visible', fontVariantNumeric: 'tabular-nums' }}>
          {/* row captions */}
          <text x={0} y={12} fontSize={12} style={fillOf('--axis-ink')}>
            Fire satellites over the site{model.total ? `, with ${number(model.total)} detections` : ''}
          </text>
          {hasGeo ? (
            <text x={0} y={rowTop + STRIP_H + 6 + 12} fontSize={12} style={fillOf('--axis-ink')}>
              {geoLabel ?? 'Geostationary GK-2A, fire pixels per hour'}
            </text>
          ) : null}

          {/* hour grid */}
          {hourLabels.map((_, i) =>
            i % step === 0 ? <line key={`g${i}`} x1={(i / 24) * W} x2={(i / 24) * W} y1={rowTop} y2={axisTop} strokeWidth={1} shapeRendering="crispEdges" style={strokeOf('--grid')} /> : null,
          )}

          {/* the day, dark; windows lit */}
          <rect x={0} y={rowTop} width={W} height={STRIP_H} style={fillOf('--raised')} />
          {model.gaps.map((g) => {
            const gw = x(g.end) - x(g.start);
            const span = duration(g.end - g.start);
            if (!(gw > charW(span, 11) + 16 && g.end - g.start >= 2 * 3_600_000)) return null;
            // a gap that holds the now line puts its length in the wider part beside the line (the
            // line and its "Now" label would cross it), or leaves it out when neither part fits
            const nowT = now?.getTime();
            let cx = x(g.start) + gw / 2;
            if (nowT != null && nowT > g.start && nowT < g.end) {
              const nx = x(nowT);
              const room = charW(span, 11) + 16;
              const halfW = room / 2;
              if (Math.abs(cx - nx) < halfW + charW(`Now ${timeIST(now)}`, 11) + 8) {
                const leftW = nx - x(g.start);
                const rightW = x(g.end) - nx - charW(`Now ${timeIST(now)}`, 11) - 8;
                if (leftW >= rightW && leftW >= room) cx = x(g.start) + leftW / 2;
                else if (rightW >= room) cx = x(g.end) - rightW / 2;
                else return null;
              }
            }
            return (
              <text key={`gap${g.start}`} x={cx} y={rowTop + STRIP_H / 2 + 4} fontSize={11} textAnchor="middle" style={fillOf('--axis-ink')}>
                {span}
              </text>
            );
          })}
          {model.clusters.map((c) => {
            const x0 = x(Math.max(model.from, c.start));
            const x1 = x(Math.min(model.to, c.end));
            const name = `${[...c.instruments].join(', ') || 'Pass'} ${timeIST(c.start)}–${timeIST(c.end)}`;
            return (
              <g key={`c${c.start}`}>
                <rect x={x0} y={rowTop} width={Math.max(2, x1 - x0)} height={STRIP_H} style={fillOf('--chart-window')}>
                  <title>{`${name}: ${c.passes.map((p) => `${p.satellite} ${timeIST(p.tca)}`).join(', ')}`}</title>
                </rect>
                {c.passes.map((p) => {
                  const px = x(Math.max(model.from, p.start));
                  return <rect key={`p${p.start}`} x={px} y={rowTop} width={Math.max(2, x(Math.min(model.to, p.end)) - px)} height={STRIP_H} opacity={0.55} style={fillOf('--chart-1')} />;
                })}
              </g>
            );
          })}

          {/* detections: one tick per 10 minutes with any, height by count */}
          {[...model.bins.entries()].map(([k, n]) => {
            const t0 = model.from + k * TICK_BIN_MS;
            const h = 6 + (STRIP_H - 10) * Math.sqrt(n / maxBin);
            return (
              <rect key={`d${k}`} x={x(t0 + TICK_BIN_MS / 2) - 1} y={rowTop + STRIP_H - h} width={2} height={h} style={fillOf('--chart-2')}>
                <title>{`${timeIST(t0)}–${timeIST(t0 + TICK_BIN_MS)} IST: ${number(n)} detection${n === 1 ? '' : 's'}`}</title>
              </rect>
            );
          })}

          {/* geostationary hourly row, its peak labelled so the row has a scale */}
          {hasGeo
            ? (geostationary ?? []).map((g) => {
                const i = (g.hour_ist - startHour + 24) % 24;
                const bw = W / 24;
                const h = Math.max(g.value > 0 ? 2 : 0, (GEO_H - 2) * (g.value / maxGeo));
                const peak = g.value === maxGeo && g.value > 0;
                return (
                  <g key={`geo${g.hour_ist}`}>
                    <rect x={i * bw + 1} y={geoTop + GEO_H - h} width={Math.max(1, bw - 2)} height={h} opacity={0.85} style={fillOf('--chart-2')}>
                      <title>{`${String(g.hour_ist).padStart(2, '0')}:00 IST: ${number(g.value, { decimals: g.value < 10 && g.value % 1 ? 1 : 0 })} ${geoUnit}`}</title>
                    </rect>
                    {/* the peak's value: inside a tall, wide bar; above a short one; left out when it fits
                        neither (it would run into the row caption), the tooltip still has it */}
                    {peak && ((h >= 14 && bw >= 18) || h <= GEO_H - 14) ? (
                      <text
                        x={i * bw + bw / 2}
                        y={h >= 14 && bw >= 18 ? geoTop + GEO_H - h + 11 : geoTop + GEO_H - h - 3}
                        fontSize={11}
                        textAnchor="middle"
                        style={h >= 14 && bw >= 18 ? { fill: 'var(--surface, var(--bg))', fontWeight: 600 } : fillOf('--axis-ink')}
                      >
                        {number(g.value)}
                      </text>
                    ) : null}
                  </g>
                );
              })
            : null}
          {hasGeo ? <line x1={0} x2={W} y1={geoTop + GEO_H + 0.5} y2={geoTop + GEO_H + 0.5} strokeWidth={1} style={strokeOf('--grid')} /> : null}

          {/* hour labels: one step all the way, first and last pulled inside */}
          {hourLabels.map((h, i) => {
            if (i % step) return null;
            const lab = `${String(h).padStart(2, '0')}:00`;
            const tx = (i / 24) * W;
            const left = Math.max(0, Math.min(W - charW(lab, 12), tx - charW(lab, 12) / 2));
            return (
              <text key={`h${i}`} x={left} y={axisTop + 14} fontSize={12} style={fillOf('--axis-ink')}>
                {lab}
              </text>
            );
          })}

          {/* now */}
          {now && now.getTime() >= model.from && now.getTime() < model.to
            ? (() => {
                const nx = x(now.getTime());
                const lab = `Now ${timeIST(now)}`;
                const lw = charW(lab, 11);
                const right = nx + 4 + lw <= W;
                return (
                  <g>
                    <line x1={nx} x2={nx} y1={rowTop - 2} y2={axisTop} strokeWidth={1} shapeRendering="crispEdges" style={{ stroke: 'var(--text, var(--ink))' }} />
                    <text x={right ? nx + 4 : nx - 4} y={rowTop + 11} fontSize={11} textAnchor={right ? 'start' : 'end'} style={INK}>
                      {lab}
                    </text>
                  </g>
                );
              })()
            : null}
        </svg>
      ) : (
        <span className="skeleton" style={{ position: 'absolute', left: 0, right: 0, top: LABEL_H, height: STRIP_H }} />
      )}
    </div>
  );
}
