import type { Metadata } from 'next';
import { Suspense } from 'react';

import { FindingLine, FindingSkeleton } from '@/components/live/FindingLine';
import { LiveScreen } from '@/components/live/LiveScreen';
import type { LiveHeadline, ScarsSummaryLive } from '@/components/live/types';
import { PATHS } from '@/lib/api';
import { serverGet } from '@/lib/api-server';

export const metadata: Metadata = {
  title: 'Live stubble burning map: Punjab and Haryana fires',
  description:
    'Live map of Punjab and Haryana crop fires: NASA fire alerts, burn scars no alert matched, smoke drifting to Delhi, its air quality, and the next fire satellite pass.',
  alternates: { canonical: '/live' },
};

/** The finding line's first read, on the server: it streams in behind a skeleton of its own size,
 *  so a slow or cold API never holds up the rest of the screen. */
async function Finding() {
  const [head, summary] = await Promise.all([serverGet<LiveHeadline>(PATHS.headline), serverGet<ScarsSummaryLive>(PATHS.scarsSummary)]);
  return <FindingLine initialHeadline={head.ok ? head.data : undefined} initialSummary={summary.ok ? summary.data : undefined} />;
}

export default function LivePage() {
  return (
    <LiveScreen
      finding={
        <Suspense fallback={<FindingSkeleton />}>
          <Finding />
        </Suspense>
      }
    />
  );
}
