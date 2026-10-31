'use client';

import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

import { ticks } from '@/lib/charts';
import { dateIST, startOfIstDay } from '@/lib/format';

/**
 * A small kit for the hand-drawn SVG charts on the case, bulletin and method screens, drawn to the
 * same rules as lib/charts.ts (app-screens.md section 6 and the dataviz skill): 3-5 solid hairline
 * gridlines one step off the surface, no y-axis line, round ticks that top out above the largest
 * value, 2px lines without markers, labels in the text inks (never the series colour), tabular
 * figures, and a hover read-out on every chart.
 *
 * Text never scales with the drawing: each chart measures its own width and lays itself out in real
 * pixels, so an 11px tick is 11px on a phone too. Before the first measurement the chart renders at
 * `fallback` width with the final height, so the page does not shift when the width lands.
 */

export { ticks };

export function useWidth<T extends HTMLElement>(fallback = 640) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setWidth(Math.max(240, Math.round(el.getBoundingClientRect().width)));
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

/** IST midnights (and middays when there is room) inside [from, to], as epoch ms with a label.
 *  Labels fit the width: "Mon 26 Oct" (about 62 px at 11 px) where a day is 76 px or wider, else
 *  "26 Oct" (about 38 px), on every day that leaves 48 px between labels. */
export function istDayTicks(from: number, to: number, width: number): { t: number; label: string; major: boolean }[] {
  const out: { t: number; label: string; major: boolean }[] = [];
  const first = startOfIstDay(from);
  if (!first) return out;
  const days = (to - from) / 86_400_000;
  const perDay = width / Math.max(1, days);
  const withNoon = perDay > 150;
  const long = perDay >= 76;
  const step = long ? 1 : Math.max(1, Math.ceil(48 / perDay));
  let day = 0;
  for (let t = first.getTime(); t <= to; t += 12 * 3_600_000) {
    const major = (t - first.getTime()) % 86_400_000 === 0;
    if (t < from) continue;
    if (!major && !withNoon) continue;
    if (major && day++ % step !== 0) continue;
    out.push({ t, label: major ? dateIST(t, { weekday: long }) : '12:00', major });
  }
  return out;
}

/** The hover read-out: a bare box that follows the pointer inside the chart's own frame. */
export function Readout({ x, width, children }: { x: number | null; width: number; children: ReactNode }) {
  if (x == null) return null;
  const left = Math.max(0, Math.min(width - 180, x + 12));
  return (
    <div className="chart-readout nums" style={{ left }} role="presentation">
      {children}
    </div>
  );
}

/** Horizontal gridlines with their tick labels in a lane on the left. */
export function YGrid({ values, y, left, right, format }: { values: number[]; y: (v: number) => number; left: number; right: number; format: (v: number) => string }) {
  return (
    <g>
      {values.map((v) => (
        <g key={v}>
          <line x1={left} x2={right} y1={y(v)} y2={y(v)} stroke="var(--grid)" strokeWidth={1} shapeRendering="crispEdges" />
          <text x={left - 8} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--axis-ink)">
            {format(v)}
          </text>
        </g>
      ))}
    </g>
  );
}

/** Width of a tick label column for these values (an estimate at 11px tabular digits). */
export const labelLane = (labels: string[]) => 10 + Math.max(...labels.map((l) => l.length)) * 6.6;
