import type { Metadata } from 'next';
import { Suspense } from 'react';

import { BriefIndex } from '@/components/brief/BriefIndex';
import { IndexSkeleton } from '@/components/brief/BriefSkeleton';
import { getBriefIndex } from '@/components/brief/data';
import { BriefError } from '@/components/brief/parts';
import { indexHref, parseDays, parseLang } from '@/components/brief/read';

/**
 * /brief: the districts ranked by candidate unseen burns in a window of Sentinel-2 passes
 * (?days=3|7|14|30, default 7) and each one's CSV (?lang=en|hi|pa). The frame and a skeleton of the
 * real layout are prerendered; the table streams in when the API answers (serverGet keeps a good
 * answer for 30 s).
 */

export const metadata: Metadata = {
  title: 'Field brief',
  // a link preview travels without the page's audit sentence, so it says "candidate" and "upper bounds" itself
  description:
    'Where field teams could go first: the villages of Punjab and Haryana where Sentinel-2 mapped candidate burn scars that no fire alert explains (upper bounds), and the harvested fields ranked highest for the next burn, district by district.',
};

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

async function IndexBody({ searchParams }: Props) {
  const sp = await searchParams;
  const days = parseDays(sp.days);
  const lang = parseLang(sp.lang);
  const r = await getBriefIndex(days);
  if (!r.ok) return <BriefError title="Field brief" what="the field brief" message={r.error.message} retryHref={indexHref(days, lang)} />;
  return <BriefIndex data={r.data} days={days} lang={lang} />;
}

export default function BriefPage({ searchParams }: Props) {
  return (
    <Suspense fallback={<IndexSkeleton />}>
      <IndexBody searchParams={searchParams} />
    </Suspense>
  );
}
