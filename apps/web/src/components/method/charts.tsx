'use client';

import { useState } from 'react';

import { labelLane, Readout, ticks, useWidth, YGrid } from '@/components/case/chart-kit';
import { dateIST, number, percent } from '@/lib/format';

import type { AucPair, GeoDiurnal, OrbitValidation, SmokeCityRow } from './types';

/**
 * The method page's charts, each answering one question with its scale, its dates and a hover
 * read-out, drawn to the dataviz rules: one axis per chart, thin marks, solid hairline grids, text in
 * the text inks, identity never by colour alone (shape or position carries it too). Fires and GK-2A
 * pixels are the fire mark; candidate unseen burns and BlindSpot's own model are gold; references
 * and baselines are neutral.
 */

const sign = (v: number | null | undefined, d = 1) => number(v, { decimals: d, signed: true });

// ---------------------------------------------------------------------------------------------
// GK-2A activity by IST hour, with the polar satellites' windows lit

export function DiurnalChart({ data }: { data: GeoDiurnal }) {
  const [ref, width] = useWidth<HTMLDivElement>(720);
  const [hover, setHover] = useState<number | null>(null);
  const vals = data.hourly_ist.map((h) => h.mean_count ?? 0);
  const yt = ticks(0, Math.max(1, ...vals), 4);
  const top = yt[yt.length - 1];
  const left = labelLane(yt.map((v) => number(v)));
  const right = width - 4;
  const laneH = 22;
  const padT = laneH + 10;
  const plotH = 220;
  const h = padT + plotH + 28;
  const x = (min: number) => left + (min / 1440) * (right - left);
  const y = (v: number) => padT + plotH * (1 - v / top);
  const band = (right - left) / 24;
  const narrow = width < 560;
  const hh = hover == null ? null : data.hourly_ist[hover];
  const inWindow = (hour: number) => data.polar_windows_ist.filter((w) => w.start_min < (hour + 1) * 60 && w.end_min > hour * 60);

  return (
    <div ref={ref} className="chart-frame" onPointerLeave={() => setHover(null)}>
      <svg
        width={width}
        height={h}
        role="img"
        aria-label={`GK-2A fire pixels per 10-minute scan by hour of the day in IST, mean over ${data.days} days; peak ${data.peak_ist ?? 'not stated'}; polar satellite windows shaded.`}
        onPointerMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const i = Math.floor((e.clientX - r.left - left) / band);
          setHover(i >= 0 && i < 24 ? i : null);
        }}
      >
        {data.polar_windows_ist.map((w) => (
          <rect key={`${w.start}-${w.end}`} x={x(w.start_min)} y={padT} width={Math.max(2, x(w.end_min) - x(w.start_min))} height={plotH} fill="var(--chart-window)" />
        ))}
        <YGrid values={yt} y={y} left={left} right={right} format={(v) => number(v)} />
        {data.hourly_ist.map((hr, i) => {
          const v = hr.mean_count;
          if (v == null) return null;
          const bw = Math.max(2, band * 0.62);
          return <rect key={hr.hour} x={x(hr.hour * 60) + (band - bw) / 2} y={y(v)} width={bw} height={Math.max(0, y(0) - y(v))} fill="var(--mark-fire)" opacity={hover == null || hover === i ? 1 : 0.55} shapeRendering="crispEdges" />;
        })}
        {/* the lane above the plot names the windows; on a phone it says only that they are lit */}
        {narrow ? (
          <text x={left} y={14} fontSize={11} fill="var(--axis-ink)">
            Shaded: a polar fire satellite overhead
          </text>
        ) : (
          data.polar_windows_ist.map((w, k) => {
            const label = w.satellites.length > 1 ? `${w.satellites.length} satellites` : w.satellites[0];
            const prev = data.polar_windows_ist[k - 1];
            const crowded = prev && x(w.start_min) - x(prev.start_min) < 90;
            return crowded ? null : (
              <g key={`l-${w.start}`}>
                <line x1={x(w.start_min)} x2={x(w.start_min)} y1={18} y2={padT} stroke="var(--axis-ink)" strokeWidth={1} opacity={0.5} />
                <text x={x(w.start_min) + 4} y={14} fontSize={11} fill="var(--axis-ink)">
                  {label}
                </text>
              </g>
            );
          })
        )}
        {[0, 3, 6, 9, 12, 15, 18, 21].filter((hr) => !narrow || hr % 6 === 0).map((hr) => (
          <text key={hr} x={x(hr * 60)} y={h - 8} fontSize={11} fill="var(--axis-ink)">
            {`${String(hr).padStart(2, '0')}:00`}
          </text>
        ))}
        <text x={right} y={h - 8} fontSize={11} fill="var(--axis-ink)" textAnchor="end">
          IST
        </text>
      </svg>
      <Readout x={hover == null ? null : x(hover * 60 + 60)} width={width}>
        {hh ? (
          <>
            <div className="chart-readout__head">{`${String(hh.hour).padStart(2, '0')}:00 to ${String((hh.hour + 1) % 24).padStart(2, '0')}:00 IST`}</div>
            <div>{hh.mean_count == null ? 'No scans' : `${number(hh.mean_count, { decimals: 1 })} fire pixels per scan`}</div>
            <div>{`${number(hh.scans)} scans`}</div>
            <div>{inWindow(hh.hour).length ? `Polar pass: ${inWindow(hh.hour).map((w) => `${w.satellites.join(', ')} ${w.start}–${w.end}`).join('; ')}` : 'No polar fire satellite overhead'}</div>
          </>
        ) : null}
      </Readout>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// predicted pass time minus FIRMS acquisition time, per satellite

export function PassErrorChart({ data }: { data: OrbitValidation }) {
  const [ref, width] = useWidth<HTMLDivElement>(720);
  const [hover, setHover] = useState<number | null>(null);
  const rows = data.satellites.filter((s) => s.error_minutes?.n);
  const ext = Math.max(1, ...rows.flatMap((s) => [Math.abs(s.error_minutes.min_min ?? 0), Math.abs(s.error_minutes.max_min ?? 0)]));
  const lim = Math.ceil(ext);
  const left = Math.min(150, Math.max(96, width * 0.22));
  const rightLane = 64;
  const right = width - rightLane;
  const rowH = 40;
  const padT = 8;
  const h = padT + rows.length * rowH + 30;
  const x = (m: number) => left + ((m + lim) / (2 * lim)) * (right - left);
  const xt = Array.from({ length: 2 * lim + 1 }, (_, i) => i - lim);
  const hr = hover == null ? null : rows[hover];

  return (
    <div ref={ref} className="chart-frame" onPointerLeave={() => setHover(null)}>
      <svg width={width} height={h} role="img" aria-label={`Pass time error in minutes per satellite: overall median ${data.overall?.median_min ?? 'not stated'} min${data.overall?.n != null ? ` over ${data.overall.n} detections` : ''}.`}>
        {xt.map((v) => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={padT} y2={h - 26} stroke="var(--grid)" strokeWidth={v === 0 ? 1.5 : 1} opacity={v === 0 ? 1 : 0.7} shapeRendering="crispEdges" />
            <text x={x(v)} y={h - 8} fontSize={11} textAnchor="middle" fill="var(--axis-ink)">
              {v === 0 ? (width < 500 ? '0' : '0 min') : sign(v, 0)}
            </text>
          </g>
        ))}
        {rows.map((s, i) => {
          const e = s.error_minutes;
          const cy = padT + i * rowH + rowH / 2;
          return (
            <g key={s.satellite} onPointerEnter={() => setHover(i)}>
              <rect x={0} y={cy - rowH / 2} width={width} height={rowH} fill="transparent" />
              <text x={0} y={cy + 4} fontSize={13} fill="var(--ink)">
                {s.satellite}
                <tspan fill="var(--axis-ink)" fontSize={11}>{` ${s.instrument}`}</tspan>
              </text>
              {e.min_min != null && e.max_min != null ? <line x1={x(e.min_min)} x2={x(e.max_min)} y1={cy} y2={cy} stroke="var(--chart-muted)" strokeWidth={6} strokeLinecap="round" /> : null}
              {e.median_min != null ? <circle cx={x(e.median_min)} cy={cy} r={6} fill="var(--accent)" stroke="var(--bg)" strokeWidth={2} /> : null}
              <text x={width} y={cy + 4} fontSize={12} textAnchor="end" fill="var(--axis-ink)">
                {`n ${number(e.n)}`}
              </text>
            </g>
          );
        })}
      </svg>
      <Readout x={hr ? x(hr.error_minutes.max_min ?? 0) : null} width={width}>
        {hr ? (
          <>
            <div className="chart-readout__head">{`${hr.satellite} ${hr.instrument}, ${number(hr.error_minutes.n)} detections`}</div>
            <div>{`Median ${sign(hr.error_minutes.median_min, 2)} min, mean ${sign(hr.error_minutes.mean_min, 2)} min`}</div>
            <div>{`Range ${sign(hr.error_minutes.min_min, 2)} to ${sign(hr.error_minutes.max_min, 2)} min`}</div>
            <div>{`95% within ±${number(hr.error_minutes.p95_abs_min, { decimals: 2 })} min`}</div>
          </>
        ) : null}
      </Readout>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// audited shares with their 95% intervals

export type ForestRow = { label: string; value: number; ci: [number, number] | null; n: string; lead?: boolean };

export function AuditForest({ rows }: { rows: ForestRow[] }) {
  const [ref, width] = useWidth<HTMLDivElement>(720);
  const narrow = width < 560;
  const labelW = narrow ? 0 : Math.min(300, width * 0.36);
  // On a phone the label takes its own line and the read-out sits at the end of the interval's line,
  // so neither can run into the other.
  const right = width - (narrow ? 104 : 150);
  const rowH = narrow ? 64 : 48;
  const padT = 4;
  const h = padT + rows.length * rowH + 28;
  const x = (v: number) => labelW + 8 + v * (right - labelW - 8);
  return (
    <div ref={ref} className="chart-frame">
      <svg width={width} height={h} role="img" aria-label={rows.map((r) => `${r.label}: ${percent(r.value)}${r.ci ? `, 95% interval ${percent(r.ci[0])} to ${percent(r.ci[1])}` : ''}, ${r.n}`).join('; ')}>
        {[0, 0.25, 0.5, 0.75, 1].map((v) => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={padT} y2={h - 26} stroke="var(--grid)" strokeWidth={1} shapeRendering="crispEdges" />
            <text x={x(v)} y={h - 8} fontSize={11} textAnchor="middle" fill="var(--axis-ink)">
              {percent(v)}
            </text>
          </g>
        ))}
        {rows.map((r, i) => {
          const top = padT + i * rowH;
          const cy = narrow ? top + 40 : top + rowH / 2;
          const col = r.lead ? 'var(--accent)' : 'var(--ink)';
          return (
            <g key={r.label}>
              <title>{`${r.label}: ${percent(r.value)}${r.ci ? ` (95% CI ${percent(r.ci[0])}–${percent(r.ci[1])})` : ''}, ${r.n}`}</title>
              <text x={narrow ? 0 : 0} y={narrow ? top + 16 : cy + 4} fontSize={13} fill="var(--ink)">
                {r.label}
              </text>
              {r.ci ? <line x1={x(r.ci[0])} x2={x(r.ci[1])} y1={cy} y2={cy} stroke={col} strokeWidth={2} /> : null}
              {r.ci ? (
                <>
                  <line x1={x(r.ci[0])} x2={x(r.ci[0])} y1={cy - 6} y2={cy + 6} stroke={col} strokeWidth={2} />
                  <line x1={x(r.ci[1])} x2={x(r.ci[1])} y1={cy - 6} y2={cy + 6} stroke={col} strokeWidth={2} />
                </>
              ) : null}
              <circle cx={x(r.value)} cy={cy} r={7} fill={col} stroke="var(--bg)" strokeWidth={2} />
              <text x={narrow ? width : right + 16} y={cy + 4} fontSize={12} textAnchor={narrow ? 'end' : 'start'} fill="var(--axis-ink)">
                {`${percent(r.value)} · ${r.n}`}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// smoke model against measured PM2.5, per city, with the two baselines it has to beat

export function SmokeDots({ rows }: { rows: SmokeCityRow[] }) {
  const [ref, width] = useWidth<HTMLDivElement>(720);
  const [hover, setHover] = useState<number | null>(null);
  const all = rows.flatMap((r) => [r.model, r.cams, r.ventilation]).filter((v): v is number => v != null);
  const lo = Math.min(0, Math.floor(Math.min(...all) * 4) / 4);
  const hi = 1;
  const left = Math.min(140, Math.max(96, width * 0.18));
  const right = width - 64;
  const rowH = 28;
  const padT = 6;
  const h = padT + rows.length * rowH + 30;
  const x = (v: number) => left + ((v - lo) / (hi - lo)) * (right - left);
  const xt: number[] = [];
  for (let v = lo; v <= hi + 1e-9; v += 0.25) xt.push(+v.toFixed(2));
  const hr = hover == null ? null : rows[hover];
  return (
    <div ref={ref} className="chart-frame" onPointerLeave={() => setHover(null)}>
      <svg width={width} height={h} role="img" aria-label={`Correlation r with measured PM2.5 for ${rows.length} cities: BlindSpot smoke model, CAMS model and ventilation alone.`}>
        {xt.map((v) => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={padT} y2={h - 26} stroke="var(--grid)" strokeWidth={v === 0 ? 1.5 : 1} shapeRendering="crispEdges" />
            <text x={x(v)} y={h - 8} fontSize={11} textAnchor="middle" fill="var(--axis-ink)">
              {v === 0 ? 'r = 0' : number(v, { decimals: 2 }).replace('−', '−')}
            </text>
          </g>
        ))}
        {rows.map((r, i) => {
          const cy = padT + i * rowH + rowH / 2;
          return (
            <g key={r.city} onPointerEnter={() => setHover(i)}>
              <rect x={0} y={cy - rowH / 2} width={width} height={rowH} fill={hover === i ? 'var(--chart-hover)' : 'transparent'} />
              <text x={0} y={cy + 4} fontSize={13} fill="var(--ink)">
                {r.city}
              </text>
              {r.ventilation != null ? <circle cx={x(r.ventilation)} cy={cy} r={5} fill="none" stroke="var(--axis-ink)" strokeWidth={1.5} /> : null}
              {r.cams != null ? <rect x={x(r.cams) - 4.5} y={cy - 4.5} width={9} height={9} transform={`rotate(45 ${x(r.cams)} ${cy})`} fill="var(--ink)" /> : null}
              {r.model != null ? <circle cx={x(r.model)} cy={cy} r={6} fill="var(--accent)" stroke="var(--bg)" strokeWidth={1.5} /> : null}
              <text x={width} y={cy + 4} fontSize={11} textAnchor="end" fill="var(--axis-ink)">
                {r.nEff == null ? '' : `n eff ${number(r.nEff, { decimals: 1 })}`}
              </text>
            </g>
          );
        })}
      </svg>
      <Readout x={hr && hr.model != null ? x(hr.model) : null} width={width}>
        {hr ? (
          <>
            <div className="chart-readout__head">{`${hr.city}${hr.hours != null ? `, ${number(hr.hours)} hours observed` : ''}`}</div>
            <div>{`BlindSpot smoke model r ${hr.model == null ? '—' : number(hr.model, { decimals: 2 })}`}</div>
            <div>{`CAMS model r ${hr.cams == null ? '—' : number(hr.cams, { decimals: 2 })}`}</div>
            <div>{`Ventilation alone r ${hr.ventilation == null ? '—' : number(hr.ventilation, { decimals: 2 })}`}</div>
            <div>{`Effective sample ${hr.nEff == null ? '—' : number(hr.nEff, { decimals: 1 })} independent hours`}</div>
          </>
        ) : null}
      </Readout>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// risk ranking AUC for every pass pair, by the date of the later pass

export function AucStrip({ pairs, mean, lead }: { pairs: AucPair[]; mean: number | null; lead: { t0: string; t1: string } | null }) {
  const [ref, width] = useWidth<HTMLDivElement>(720);
  const [hover, setHover] = useState<number | null>(null);
  const pts = pairs.filter((p) => p.auc != null).map((p) => ({ ...p, ms: Date.parse(p.t1) }));
  if (!pts.length) return null;
  const t0 = Math.min(...pts.map((p) => p.ms)) - 86_400_000;
  const t1 = Math.max(...pts.map((p) => p.ms)) + 86_400_000;
  const yt = [0, 0.25, 0.5, 0.75, 1];
  const left = labelLane(yt.map((v) => number(v, { decimals: 2 })));
  const right = width - 8;
  const padT = 24;
  const plotH = 220;
  const h = padT + plotH + 28;
  const x = (ms: number) => left + ((ms - t0) / (t1 - t0)) * (right - left);
  const y = (v: number) => padT + plotH * (1 - v);
  const isLead = (p: AucPair) => !!lead && p.t0 === lead.t0 && p.t1 === lead.t1;
  const weeks: number[] = [];
  for (let d = new Date(t0); d.getTime() <= t1; d = new Date(d.getTime() + 86_400_000)) {
    const ist = new Date(d.getTime() + 5.5 * 3_600_000);
    if (ist.getUTCDay() === 1) weeks.push(d.getTime());
  }
  const hp = hover == null ? null : pts[hover];
  return (
    <div ref={ref} className="chart-frame" onPointerLeave={() => setHover(null)}>
      <svg
        width={width}
        height={h}
        role="img"
        aria-label={`Area under the curve of the burn-risk ranking for ${pts.length} pass pairs, ${dateIST(t0 + 86_400_000)} to ${dateIST(t1 - 86_400_000)}; weighted mean ${mean == null ? 'not stated' : number(mean, { decimals: 3 })}; 0.5 is a random ranking.`}
        onPointerMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const px = e.clientX - r.left;
          const py = e.clientY - r.top;
          let best = -1;
          let bd = 400;
          pts.forEach((p, i) => {
            const d = (x(p.ms) - px) ** 2 + (y(p.auc as number) - py) ** 2;
            if (d < bd) {
              bd = d;
              best = i;
            }
          });
          setHover(best >= 0 ? best : null);
        }}
      >
        <YGrid values={yt} y={y} left={left} right={right} format={(v) => number(v, { decimals: 2 })} />
        <line x1={left} x2={right} y1={y(0.5)} y2={y(0.5)} stroke="var(--axis-ink)" strokeWidth={1} />
        <text x={left + 6} y={y(0.5) + 15} fontSize={11} fill="var(--axis-ink)">
          0.5: a random ranking
        </text>
        {mean != null ? (
          <>
            <line x1={left} x2={right} y1={y(mean)} y2={y(mean)} stroke="var(--accent)" strokeWidth={1.5} />
            <text x={left + 6} y={y(mean) - 6} fontSize={11} fill="var(--accent)">
              {`weighted mean ${number(mean, { decimals: 3 })}`}
            </text>
          </>
        ) : null}
        {pts.map((p, i) => (
          <circle key={i} cx={x(p.ms)} cy={y(p.auc as number)} r={isLead(p) ? 6 : 3.5} fill={isLead(p) ? 'var(--accent)' : 'var(--chart-1)'} fillOpacity={isLead(p) ? 1 : 0.55} stroke={isLead(p) || hover === i ? 'var(--ink)' : 'var(--bg)'} strokeWidth={isLead(p) || hover === i ? 2 : 1} />
        ))}
        {weeks.map((t) => (
          <text key={t} x={x(t)} y={h - 8} fontSize={11} textAnchor="middle" fill="var(--axis-ink)">
            {dateIST(t)}
          </text>
        ))}
      </svg>
      <Readout x={hp ? x(hp.ms) : null} width={width}>
        {hp ? (
          <>
            <div className="chart-readout__head">{`${dateIST(hp.t0)} to ${dateIST(hp.t1)}`}</div>
            <div>{`AUC ${number(hp.auc, { decimals: 3 })}`}</div>
            <div>{`${number(hp.burnt)} fields burnt, ${number(hp.unburnt)} not`}</div>
            <div>{`Base rate ${percent(hp.base_rate, 1)}`}</div>
          </>
        ) : null}
      </Readout>
    </div>
  );
}
