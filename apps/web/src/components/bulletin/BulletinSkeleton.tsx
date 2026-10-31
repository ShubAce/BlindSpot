import { Skeleton } from '@/components/ui/states';

/** The bulletin's final layout with flat bars where the text will land (no shimmer, no shift). */
export function BulletinSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading the bulletin…">
      <div className="title-row">
        <div className="title-row__text">
          <Skeleton width="min(460px, 90%)" height={32} />
          <Skeleton width="min(680px, 95%)" height={20} style={{ marginTop: 12 }} />
        </div>
      </div>
      <div className="lang-tabs">
        {[72, 56, 56].map((w, i) => (
          <Skeleton key={i} width={w} height={18} style={{ margin: '11px 0' }} />
        ))}
      </div>
      <div className="bulletin-grid">
        <div className="bulletin">
          <Skeleton width="90%" height={30} />
          <Skeleton width="100%" height={22} style={{ marginTop: 20 }} />
          <Skeleton width="80%" height={22} style={{ marginTop: 8 }} />
          {[0, 1, 2, 3].map((i) => (
            <div key={i} style={{ marginTop: 36 }}>
              <Skeleton width={220} height={22} />
              <Skeleton width="100%" height={18} style={{ marginTop: 12 }} />
              <Skeleton width="96%" height={18} style={{ marginTop: 8 }} />
              <Skeleton width="70%" height={18} style={{ marginTop: 8 }} />
            </div>
          ))}
        </div>
        <div className="bulletin-rail">
          <section className="card">
            <Skeleton width={180} height={20} />
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <Skeleton key={i} height={16} style={{ marginTop: 14 }} />
            ))}
          </section>
        </div>
      </div>
    </div>
  );
}
