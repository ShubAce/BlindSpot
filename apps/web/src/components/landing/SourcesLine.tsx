'use client';

import { ErrorNote } from '@/components/ui/states';
import type { SourcesResponse } from '@/lib/api';
import { number, relative, toDate } from '@/lib/format';
import { useNow, useSources } from '@/lib/live';

import { s } from './styles';
import { shortLabel } from './words';

const HOUR_MS = 3_600_000;

/**
 * "21 of 24 sources are on schedule; 12 answered in the last hour." Live from GET /api/sources,
 * polled every minute. "On schedule" is the backend's own rule (status ok: not three runs late).
 */
export function SourcesLine({ initial }: { initial: SourcesResponse | null }) {
  const q = useSources({ fallbackData: initial ?? undefined });
  const now = useNow(30_000);
  const list = q.data?.sources ?? [];
  if (!q.data) {
    return q.error ? <ErrorNote what="the source status" error={q.error} onRetry={() => void q.mutate()} /> : null;
  }
  const ok = list.filter((x) => x.status === 'ok').length;
  const late = list.filter((x) => x.status === 'stale' || x.status === 'error');
  const recent = now ? list.filter((x) => {
    const t = toDate(x.last_success_at);
    return t != null && now.getTime() - t.getTime() <= HOUR_MS;
  }).length : null;
  return (
    <p className={s.statusLine} aria-live="polite">
      {`${number(ok)} of ${number(list.length)} sources are on schedule`}
      {recent != null ? `; ${number(recent)} answered in the last hour` : ''}
      {late.length ? `. Running late: ${late.map((x) => shortLabel(x.label)).join(', ')}` : ''}
      {'. '}
      {now && q.data.generated_at ? `Checked ${relative(Math.min(Date.parse(q.data.generated_at), now.getTime()), now)}.` : ''}
    </p>
  );
}
