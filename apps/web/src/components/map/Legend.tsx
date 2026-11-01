import type { CSSProperties, ReactNode } from 'react';

import type { LegendItem, Mark } from './marks';
import { cssColor, type RGBA } from './palette';

const W = 16;
const H = 12;

/** One mark, drawn at swatch size from its own style. */
export function LegendSwatch({ mark, size = 1, className }: { mark: Mark; size?: number; className?: string }) {
  const w = W * size;
  const h = H * size;
  const c = (x: RGBA) => cssColor(x);
  let body: ReactNode = null;
  switch (mark.kind) {
    case 'dot':
      body = <circle cx={w / 2} cy={h / 2} r={Math.min(mark.radius, h / 2 - 0.5)} fill={c(mark.fill)} stroke={mark.stroke ? c(mark.stroke) : undefined} strokeWidth={mark.strokeWidth} />;
      break;
    case 'ring':
      body = <circle cx={w / 2} cy={h / 2} r={Math.min(mark.radius, h / 2 - mark.strokeWidth)} fill="none" stroke={c(mark.stroke)} strokeWidth={mark.strokeWidth} />;
      break;
    case 'square': {
      const s = h - 2 - mark.strokeWidth;
      body = <rect x={(w - s) / 2} y={(h - s) / 2} width={s} height={s} fill={mark.fill ? c(mark.fill) : 'none'} stroke={c(mark.stroke)} strokeWidth={mark.strokeWidth} />;
      break;
    }
    case 'area':
      body = <rect x={1} y={1.5} width={w - 2} height={h - 3} fill={c(mark.fill)} stroke={mark.stroke ? c(mark.stroke) : undefined} strokeWidth={mark.strokeWidth} />;
      break;
    case 'outline':
      body = <rect x={1} y={1.5} width={w - 2} height={h - 3} fill={mark.fill ? c(mark.fill) : 'none'} stroke={c(mark.stroke)} strokeWidth={mark.strokeWidth} />;
      break;
    case 'line':
      body = <line x1={0} x2={w} y1={h / 2} y2={h / 2} stroke={c(mark.color)} strokeWidth={mark.width} strokeDasharray={mark.dash?.join(' ')} />;
      break;
    case 'trail': {
      // the TripsLayer fade, in three steps of the mark's own alpha (not a gradient)
      const [r, g, b, a] = mark.color;
      body = (
        <g strokeWidth={mark.width} strokeLinecap="round">
          <line x1={1} x2={w / 3} y1={h / 2} y2={h / 2} stroke={c([r, g, b, Math.round(a * 0.25)])} />
          <line x1={w / 3} x2={(2 * w) / 3} y1={h / 2} y2={h / 2} stroke={c([r, g, b, Math.round(a * 0.6)])} />
          <line x1={(2 * w) / 3} x2={w - 1} y1={h / 2} y2={h / 2} stroke={c(mark.color)} />
        </g>
      );
      break;
    }
    case 'hex': {
      const r = ((h - 1) / 2) * (mark.scale ?? 1);
      const pts = Array.from({ length: 6 }, (_, i) => {
        const a = (Math.PI / 180) * (60 * i - 30);
        return `${(w / 2 + r * Math.cos(a)).toFixed(2)},${(h / 2 + r * Math.sin(a)).toFixed(2)}`;
      }).join(' ');
      body = <polygon points={pts} fill={c(mark.fill)} stroke={mark.stroke ? c(mark.stroke) : undefined} strokeWidth={mark.strokeWidth} />;
      break;
    }
    case 'band':
      body = (
        <g>
          <rect x={0} y={2} width={w} height={h - 4} fill={c(mark.fill)} />
          <line x1={0} x2={w} y1={2} y2={2} stroke={c(mark.edge)} strokeWidth={1} />
          <line x1={0} x2={w} y1={h - 2} y2={h - 2} stroke={c(mark.edge)} strokeWidth={1} />
        </g>
      );
      break;
  }
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" className={className} style={{ flex: 'none', overflow: 'visible' }}>
      {body}
    </svg>
  );
}

const listStyle: CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexWrap: 'wrap',
  gap: '4px 14px',
  fontSize: 12,
  lineHeight: '16px',
  color: 'var(--muted)',
};
const itemStyle: CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' };

/** A layer's legend: swatches with grey labels, as a list (put it in the panel header). */
export function Legend({ items, className, style, vertical = false }: { items: LegendItem[]; className?: string; style?: CSSProperties; vertical?: boolean }) {
  if (!items.length) return null;
  return (
    <ul className={className} style={{ ...listStyle, ...(vertical ? { flexDirection: 'column', gap: 4 } : null), ...style }}>
      {items.map((it) => (
        <li key={it.label} style={itemStyle} title={it.note}>
          <LegendSwatch mark={it.mark} />
          <span>{it.label}</span>
        </li>
      ))}
    </ul>
  );
}
