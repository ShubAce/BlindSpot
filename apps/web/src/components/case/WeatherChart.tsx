'use client';

import { useState } from 'react';

import { compass, dateTimeIST, metricText, number } from '@/lib/format';

import { istDayTicks, labelLane, Readout, ticks, useWidth, YGrid } from './chart-kit';
import type { CaseWeatherPoint } from './types';

/**
 * The weather between the two passes that bracket the burn, from the met model the API names: the
 * 10 m wind (speed as a line, the direction it blows towards as a small arrow over each reading) and
 * the boundary-layer (mixing) height. Two panels, one x axis in IST, never two y scales on one plot.
 * The window's two ends are the two Sentinel-2 passes.
 */

type Panel = { key: 'wind_10m_kmh' | 'blh_m'; title: string; unit: string; height: number };
const PANELS: Panel[] = [
  { key: 'wind_10m_kmh', title: 'Wind at 10 m', unit: 'km/h', height: 150 },
  { key: 'blh_m', title: 'Mixing height', unit: 'm', height: 130 },
];

export function WeatherChart({ series, from, to }: { series: CaseWeatherPoint[]; from: string; to: string }) {
  const [ref, width] = useWidth<HTMLDivElement>(640);
  const [hover, setHover] = useState<number | null>(null);
  const t0 = Date.parse(from);
  const t1 = Date.parse(to);
  const pts = series.map((p) => ({ ...p, ms: Date.parse(p.t) })).filter((p) => Number.isFinite(p.ms));
  if (!pts.length || !Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) return null;

  const allLabels = PANELS.flatMap((pn) => ticks(0, Math.max(1, ...pts.map((p) => p[pn.key] ?? 0)), 3).map((v) => number(v)));
  const left = labelLane(allLabels);
  const right = width - 8;
  const x = (ms: number) => left + ((ms - t0) / (t1 - t0)) * (right - left);
  const xt = istDayTicks(t0, t1, right - left);
  const nearest = (px: number) => {
    let best = 0;
    pts.forEach((p, i) => {
      if (Math.abs(x(p.ms) - px) < Math.abs(x(pts[best].ms) - px)) best = i;
    });
    return best;
  };
  const hp = hover == null ? null : pts[hover];

  return (
    <div ref={ref} className="chart-frame" onPointerLeave={() => setHover(null)}>
      {PANELS.map((pn, k) => {
        const vals = pts.map((p) => p[pn.key]);
        const yt = ticks(0, Math.max(1, ...vals.map((v) => v ?? 0)), 3);
        const top = yt[yt.length - 1];
        const arrowLane = pn.key === 'wind_10m_kmh' ? 22 : 0;
        const padT = 8 + arrowLane;
        const padB = k === PANELS.length - 1 ? 26 : 10;
        const h = pn.height + padB;
        const y = (v: number) => padT + (pn.height - padT) * (1 - v / top);
        const d = pts.reduce((acc, p, i) => {
          const v = p[pn.key];
          if (v == null) return acc;
          const prev = i > 0 ? pts[i - 1][pn.key] : null;
          return `${acc}${prev == null ? 'M' : 'L'}${x(p.ms).toFixed(1)} ${y(v).toFixed(1)}`;
        }, '');
        return (
          <figure key={pn.key} className="chart-panel">
            <figcaption className="chart-panel__title">
              {pn.title}, {pn.unit}
            </figcaption>
            <svg
              width={width}
              height={h}
              role="img"
              aria-label={`${pn.title} from ${dateTimeIST(from)} to ${dateTimeIST(to)} IST: ${vals.filter((v) => v != null).length} readings, from ${number(Math.min(...vals.filter((v): v is number => v != null)))} to ${number(Math.max(...vals.filter((v): v is number => v != null)))} ${pn.unit}`}
              onPointerMove={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                setHover(nearest(e.clientX - r.left));
              }}
            >
              <YGrid values={yt} y={y} left={left} right={right} format={(v) => number(v)} />
              {xt.map((tk) => (
                <line key={tk.t} x1={x(tk.t)} x2={x(tk.t)} y1={padT} y2={pn.height} stroke="var(--grid)" strokeWidth={1} opacity={tk.major ? 1 : 0.5} shapeRendering="crispEdges" />
              ))}
              <path d={d} fill="none" stroke="var(--chart-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              {pn.key === 'wind_10m_kmh'
                ? pts.map((p) =>
                    p.wind_from_deg == null ? null : (
                      <g key={p.ms} transform={`translate(${x(p.ms).toFixed(1)} ${8 + arrowLane / 2 - 2}) rotate(${(p.wind_from_deg + 180) % 360})`}>
                        <path d="M0 -6 L3.5 3 L0 1 L-3.5 3 Z" fill="var(--muted)" />
                      </g>
                    ),
                  )
                : null}
              {hp && hp[pn.key] != null ? (
                <>
                  <line x1={x(hp.ms)} x2={x(hp.ms)} y1={padT} y2={pn.height} stroke="var(--axis-ink)" strokeWidth={1} opacity={0.5} />
                  <circle cx={x(hp.ms)} cy={y(hp[pn.key] as number)} r={3.5} fill="var(--surface)" stroke="var(--chart-1)" strokeWidth={2} />
                </>
              ) : null}
              {k === PANELS.length - 1
                ? xt
                    // a label that would run past the right edge is left out, not pushed onto its neighbour
                    .filter((tk) => (x(tk.t) > left + 20 || tk.major) && x(tk.t) + tk.label.length * 6.3 <= right)
                    .map((tk) => (
                      <text key={tk.t} x={x(tk.t)} y={h - 8} fontSize={11} fill="var(--axis-ink)">
                        {tk.label}
                      </text>
                    ))
                : null}
            </svg>
          </figure>
        );
      })}
      <Readout x={hp ? x(hp.ms) : null} width={width}>
        {hp ? (
          <>
            <div className="chart-readout__head">{dateTimeIST(hp.t, { suffix: true })}</div>
            <div>
              Wind {metricText('wind_kmh', hp.wind_10m_kmh)} from {compass(hp.wind_from_deg)}
            </div>
            <div>Mixing height {metricText('blh_m', hp.blh_m)}</div>
            <div>
              {metricText('temp_c', hp.temp_2m_c)}, rain {metricText('rain_mm', hp.precip_mm)}
            </div>
          </>
        ) : null}
      </Readout>
    </div>
  );
}
