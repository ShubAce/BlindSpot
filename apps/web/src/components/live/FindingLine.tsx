'use client';

import { ErrorNote, Skeleton } from '@/components/ui/states';
import { dateIST, metricText, number } from '@/lib/format';
import { CADENCE, useApi } from '@/lib/live';
import { PATHS } from '@/lib/api';

import { listJoin, precisionText, useLiveHeadline } from './data';
import styles from './styles';
import type { LiveHeadline, ScarsSummaryLive } from './types';

/**
 * The screen's one finding, computed from /api/stats/headline and /api/scars/summary: how much
 * ground burned with no fire alert, where most of it is, between which Sentinel-2 passes, and how
 * far the audit trusts the label. The server hands its first read over (no blank first paint); the
 * hooks keep it current.
 */
export function FindingLine({ initialHeadline, initialSummary }: { initialHeadline?: LiveHeadline; initialSummary?: ScarsSummaryLive }) {
  const head = useLiveHeadline(initialHeadline);
  const summary = useApi<ScarsSummaryLive>(PATHS.scarsSummary, undefined, { refreshInterval: CADENCE.scars, fallbackData: initialSummary });

  const u = head.data?.unseen_burns;
  if (head.error && !head.data) return <ErrorNote what="the headline figures" error={head.error} onRetry={() => void head.mutate()} style={{ fontSize: 15 }} />;
  if (!head.data) return <FindingSkeleton />;
  if (!u || u.available === false || u.unseen_area_ha == null) {
    return <p className={styles.finding}>Candidate unseen burns are not counted yet{u?.reason ? `: ${u.reason}` : ''}.</p>;
  }

  const top = (summary.data?.by_district ?? [])
    .filter((d) => d.unseen_area_ha > 0)
    .slice()
    .sort((a, b) => b.unseen_area_ha - a.unseen_area_ha)
    .slice(0, 3)
    .map((d) => d.district);
  const precision = precisionText(u);
  const passes = u.first_pass && u.last_pass ? ` between the passes of ${dateIST(u.first_pass)} and ${dateIST(u.last_pass)}` : '';

  return (
    <p className={styles.finding}>
      <strong className={`${styles.findingFigure} nums`}>{metricText('area_ha', u.unseen_area_ha)}</strong> of candidate unseen burns, fields Sentinel-2 saw burn with no fire alert
      {passes}
      {top.length ? <>, most in {listJoin(top)}</> : null}
      {u.districts ? (
        <>
          {' '}(<span className="nums">{number(u.districts)}</span> districts)
        </>
      ) : null}
      .{' '}
      <span className={styles.findingQual}>{precision ? `${precision.charAt(0).toUpperCase()}${precision.slice(1)}.` : 'The label has no audited precision yet.'}</span>
    </p>
  );
}

export function FindingSkeleton() {
  return (
    <div className={styles.finding} aria-hidden="true">
      <Skeleton width="92%" height={18} />
      <Skeleton width="60%" height={18} style={{ marginTop: 6 }} />
    </div>
  );
}
