'use client';

import type { CSSProperties, ReactNode } from 'react';

import { ApiError, type Envelope } from '@/lib/api';
import { relative } from '@/lib/format';
import { useFreshness, useNow } from '@/lib/live';

/**
 * The three honest states every data block has (app-screens.md section 11):
 * - loading: the final layout with each value replaced by a flat bar at its real size, no shimmer;
 * - error: one sentence inside the block that failed, when data was last good, and Retry;
 * - empty: one sentence that says what is not there (and keeps the block's frame).
 */

/** A flat bar where a value will land. Give it the value's real size (a 20px figure: height 20). */
export function Skeleton({ width = '100%', height = 16, className, style }: { width?: number | string; height?: number | string; className?: string; style?: CSSProperties }) {
  return <span aria-hidden="true" className={`skeleton${className ? ` ${className}` : ''}`} style={{ display: 'inline-block', verticalAlign: 'middle', width, height, ...style }} />;
}

const noteStyle: CSSProperties = { margin: 0, color: 'var(--muted)', fontSize: 'inherit', lineHeight: 'inherit' };

/**
 * "Couldn't load fire detections. The BlindSpot API is not reachable. Last good data 12 min ago. Retry"
 * Pass the response you still hold (SWR keeps it) as `lastGood` so the reader knows what they see.
 */
export function ErrorNote({ what, error, lastGood, onRetry, style }: { what: string; error: unknown; lastGood?: Partial<Envelope> | null; onRetry?: () => void; style?: CSSProperties }) {
  const now = useNow(30_000);
  const reason = error instanceof ApiError ? error.userMessage : 'Something went wrong on this page.';
  const at = lastGood?.generated_at;
  return (
    <p role="status" style={{ ...noteStyle, ...style }}>
      {`Couldn't load ${what}. ${reason}`}
      {at && now ? ` Showing data from ${relative(at, now)}.` : ''}
      {onRetry ? (
        <>
          {' '}
          <button type="button" className="text-button" onClick={onRetry}>
            Retry
          </button>
        </>
      ) : null}
    </p>
  );
}

/** One sentence for a block with nothing to show, e.g. "No fire alerts in the last 24 hours." */
export function EmptyNote({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return <p style={{ ...noteStyle, ...style }}>{children}</p>;
}

/** "updated 4 min ago", turning into "stale: updated 2 h ago" after three missed cadences. */
export function Freshness({ resp, cadenceMs, verb = 'updated', style }: { resp: Partial<Envelope> | null | undefined; cadenceMs?: number; verb?: string; style?: CSSProperties }) {
  const f = useFreshness(resp, cadenceMs, verb);
  if (!f) return <Skeleton width={96} height={12} />;
  return (
    <span className="nums" title={f.at.toISOString()} style={{ color: f.stale ? 'var(--warning, var(--muted))' : 'var(--faint, var(--muted))', fontSize: 12, ...style }}>
      {f.stale ? `stale: ${f.label}` : f.label}
    </span>
  );
}
