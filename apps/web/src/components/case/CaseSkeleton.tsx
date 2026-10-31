import { Skeleton } from '@/components/ui/states';

/**
 * The case screen while its file is on the way: the final layout with each value a flat bar at its
 * real size (no shimmer), so nothing moves when the data lands.
 */
export function CaseSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading the case file…">
      <div className="title-row">
        <div className="title-row__text">
          <Skeleton width={180} height={24} />
          <Skeleton width="min(520px, 90%)" height={32} style={{ marginTop: 12 }} />
          <Skeleton width="min(720px, 95%)" height={20} style={{ marginTop: 12 }} />
        </div>
      </div>
      <div className="kpis">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="kpi">
            <Skeleton width={110} height={14} />
            <Skeleton width={72} height={26} style={{ marginTop: 6 }} />
            <Skeleton width={120} height={12} style={{ marginTop: 6 }} />
          </div>
        ))}
      </div>
      <div className="case-grid">
        <section className="card span-8">
          <Skeleton width={120} height={20} />
          <div className="evidence__grid" style={{ marginTop: 16 }}>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="chip">
                <div className="chip__frame" />
              </div>
            ))}
          </div>
        </section>
        <section className="card span-4">
          <Skeleton width={140} height={20} />
          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
            <Skeleton key={i} height={16} style={{ marginTop: 14 }} />
          ))}
        </section>
      </div>
    </div>
  );
}
