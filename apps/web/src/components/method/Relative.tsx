'use client';

import { relative, dateTimeIST } from '@/lib/format';
import { useNow } from '@/lib/live';

/**
 * "4 min ago", kept current in the browser. Until the clock is known (server render, first paint) it
 * shows the IST time itself, so the server and the browser never disagree on the text.
 */
export function Relative({ iso }: { iso: string }) {
  const now = useNow(30_000);
  return (
    <time dateTime={iso} title={dateTimeIST(iso, { suffix: true })} className="nums">
      {now ? relative(iso, now) : dateTimeIST(iso, { suffix: true })}
    </time>
  );
}
