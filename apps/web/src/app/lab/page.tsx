import type { Metadata } from 'next';

import { LabClient } from './LabClient';

export const metadata: Metadata = {
  title: 'Lab',
  description: 'Developer lab: every map layer and live check, bound to the running API.',
  robots: { index: false, follow: false },
};

/** A developer route: every map layer bound to the real API, with the self-checks beside it. It
 *  holds no data of its own; with the API down it says so, block by block. */
export default function LabPage() {
  return <LabClient />;
}
