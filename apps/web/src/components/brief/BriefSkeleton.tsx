import type { CSSProperties } from 'react';

import { Skeleton } from '@/components/ui/states';

/**
 * The brief's screens while the API answers: the final layout with each value a flat bar at its real
 * size (no shimmer), so nothing moves when the data lands. What does not depend on the data (the
 * index's title) is shown at once.
 */

/** A bar on its own line (the shared Skeleton is inline, for bars inside a line of text). */
const Bar = ({ width, height, style }: { width: number | string; height: number; style?: CSSProperties }) => (
  <Skeleton width={width} height={height} style={{ display: 'block', ...style }} />
);

function KpiBars({ n }: { n: number }) {
  return (
    <div className={`kpis${n === 4 ? ' br-kpis--4' : ''}`}>
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="kpi">
          <Bar width={120} height={14} style={{ marginTop: 3 }} />
          <Bar width={84} height={26} style={{ marginTop: 4 }} />
          <Bar width={100} height={12} style={{ marginTop: 4 }} />
        </div>
      ))}
    </div>
  );
}

function RowBars({ rows }: { rows: number }) {
  return (
    <div className="br-skel-rows">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="br-skel-row">
          <Skeleton width={18} height={14} />
          <Skeleton width="min(180px, 40%)" height={14} />
          <Skeleton width="min(320px, 30%)" height={14} />
        </div>
      ))}
    </div>
  );
}

function Scope() {
  return (
    <div className="br-scope">
      <Bar width={190} height={16} />
      {/* as wide as the window switch: its options' width here, the full row on a phone (brief.css) */}
      <Skeleton width={264} height={36} className="br-skel-switch" style={{ display: 'block' }} />
    </div>
  );
}

export function IndexSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading the field brief…">
      <div className="title-row">
        <div className="title-row__text br-skel-text">
          <h1>Field brief</h1>
          <Bar width="min(700px, 95%)" height={20} style={{ marginTop: 14 }} />
          <Bar width="min(680px, 92%)" height={20} style={{ marginTop: 4 }} />
          <Bar width="min(240px, 50%)" height={20} style={{ marginTop: 4 }} />
        </div>
        <Scope />
      </div>
      <KpiBars n={4} />
      <div className="br-qual">
        <Bar width="min(900px, 95%)" height={18} />
        <Bar width="min(760px, 85%)" height={18} style={{ marginTop: 8 }} />
      </div>
      <section className="card br-card">
        <Bar width={340} height={20} />
        <Bar width={420} height={16} style={{ marginTop: 6 }} />
        <RowBars rows={12} />
      </section>
    </div>
  );
}

export function DistrictSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading the district’s field brief…">
      <div className="title-row">
        <div className="title-row__text br-skel-text">
          <Bar width={96} height={16} style={{ marginTop: 8 }} />
          <Bar width={220} height={32} style={{ marginTop: 12 }} />
          <Bar width="min(720px, 95%)" height={20} style={{ marginTop: 12 }} />
          <Bar width="min(520px, 80%)" height={20} style={{ marginTop: 4 }} />
          <Bar width="min(640px, 85%)" height={16} style={{ marginTop: 8 }} />
        </div>
        <Scope />
      </div>
      <div className="br-actions">
        <Bar width="min(400px, 100%)" height={40} />
        <Bar width={90} height={40} />
      </div>
      <KpiBars n={5} />
      <section className="card br-notes">
        <Bar width={180} height={20} />
        {[0, 1, 2, 3].map((i) => (
          <Bar key={i} width="95%" height={14} style={{ marginTop: 12 }} />
        ))}
      </section>
      <section className="card br-card">
        <Bar width={300} height={20} />
        <RowBars rows={10} />
      </section>
    </div>
  );
}
