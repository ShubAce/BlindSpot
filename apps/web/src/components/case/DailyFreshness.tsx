'use client';

import { Skeleton } from '@/components/ui/states';
import { DAILY_RUN_STALE_MS } from '@/lib/cadence';
import { useFreshness } from '@/lib/live';

/**
 * How fresh an answer built by the daily Sentinel-2 run is: "case file built 9 h ago · scars are
 * re-checked daily", in faint ink. Stale by the API's own rule for that job (DAILY_RUN_STALE_MS,
 * 26 h): only then does the label take the warning colour and say that the daily run is late.
 */
export function DailyFreshness({ resp, verb, note }: { resp: Parameters<typeof useFreshness>[0]; verb: string; note: string }) {
  const f = useFreshness(resp, undefined, verb, DAILY_RUN_STALE_MS);
  if (!f) return <Skeleton width={96} height={12} />;
  const stale = f.stale;
  return (
    <span className={`daily-fresh nums${stale ? ' is-stale' : ''}`} title={f.at.toISOString()}>
      {f.label}
      <span className="daily-fresh__note">{stale ? ' · the daily run is late' : ` · ${note}`}</span>
    </span>
  );
}
