import type { Metadata } from 'next';

import { SiteNav } from '@/components/nav/SiteNav';

import { LabClient } from './LabClient';

export const metadata: Metadata = {
  title: 'Lab',
  description: 'Developer lab: every map layer and live check, bound to the running API.',
  robots: { index: false, follow: false },
};

/** A developer route: every map layer bound to the real API, with the self-checks beside it. It
 *  holds no data of its own; with the API down it says so, block by block. It sits under the same
 *  site bar as every page (no place marked: the lab is none of the four), and its two columns fill
 *  the screen below that bar. */
export default function LabPage() {
  return (
    <>
      <SiteNav skipLabel="Skip to the lab" />
      <LabClient />
    </>
  );
}
