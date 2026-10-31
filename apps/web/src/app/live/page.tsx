import type { Metadata } from 'next';
import { Suspense } from 'react';

import { FindingLine, FindingSkeleton } from '@/components/live/FindingLine';
import { LiveScreen } from '@/components/live/LiveScreen';
import type { LiveHeadline, ScarsSummaryLive } from '@/components/live/types';
import { PATHS } from '@/lib/api';
import { serverGet } from '@/lib/api-server';

export const metadata: Metadata = {
  title: 'Mission control',
  description:
    'Live map of the Punjab and Haryana crop-fire season: burn scars no fire satellite alerted on, fire detections, where the smoke drifts, the air at Delhi, and when the next fire satellite looks.',
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
