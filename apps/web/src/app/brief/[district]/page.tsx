import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import { Suspense } from 'react';

import { DistrictSkeleton } from '@/components/brief/BriefSkeleton';
import { getDistrictBrief } from '@/components/brief/data';
import { BackToIndex, DistrictBrief } from '@/components/brief/DistrictBrief';
import { BriefError } from '@/components/brief/parts';
import { count, decodeSegment, districtHref, haText, parseDays, parseWatchAll, windowRange } from '@/components/brief/read';
import { placeName } from '@/lib/format';

/**
 * /brief/[district]: one district's field brief (?days=3|7|14|30, default 7; ?watch=all lists the
 * whole watchlist instead of its first 50 villages). The segment is the district's name in any case
 * and punctuation ("patiala", "Patiala", "sas-nagar"): the API matches it. A name the API does not
 * know renders this segment's not-found page, which lists the districts it does know; any other
 * failure says why and offers Retry, and shows no figure.
 */

type Props = {
  params: Promise<{ district: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

// A district's name is a few words; anything longer is not one, so it never reaches the API.
const plausible = (name: string) => name.trim().length > 0 && name.length <= 80;

/** The read's arguments, worked out in one place: the metadata and the body share one cached read
 *  only while they ask with exactly the same ones. */
async function briefArgs({ params, searchParams }: Props) {
  const name = decodeSegment((await params).district);
  const sp = await searchParams;
  return { name, days: parseDays(sp.days), watchAll: parseWatchAll(sp.watch) };
}

export async function generateMetadata(props: Props): Promise<Metadata> {
  const { name, days, watchAll } = await briefArgs(props);
  if (!plausible(name)) return { title: 'No brief for this district', robots: { index: false } };
  const r = await getDistrictBrief(name, days, watchAll);
  if (!r.ok) return { title: r.error.status === 404 ? 'No brief for this district' : 'Field brief' };
  const d = r.data;
  const t = d.totals;
  const where = placeName(d.district, d.state);
  const what =
    t.unseen_clusters > 0
      ? `${count(t.unseen_clusters)} candidate unseen burns (${haText(t.unseen_area_ha, 0)}, upper bounds) in ${count(t.villages)} villages`
      : 'No candidate unseen burns';
  // A link preview travels alone: it claims top-tenth fields only where the run ranks some there
  // (19 of the 44 districts of the week to 30 Oct had none).
  const watched =
    t.top_tenth_fields > 0
      ? ', and the harvested fields ranked in the top tenth for the next burn.'
      : t.harvested_fields > 0
        ? '; none of its harvested fields ranks in the top tenth for the next burn.'
        : '.';
  return {
    title: `${d.district} field brief`,
    description: `${what} of ${where}${d.window ? ` in the Sentinel-2 passes of ${windowRange(d.window)}` : ''}${watched}`,
  };
}

async function DistrictBody(props: Props) {
  const { name, days, watchAll } = await briefArgs(props);
  if (!plausible(name)) notFound();
  const r = await getDistrictBrief(name, days, watchAll);
  if (!r.ok) {
    if (r.error.status === 404 || r.error.status === 422) notFound();
    return (
      <BriefError
        eyebrow={<BackToIndex days={days} />}
        title="Field brief"
        what={`the brief for “${name}”`}
        message={r.error.message}
        retryHref={districtHref(name, days, watchAll)}
      />
    );
  }
  return <DistrictBrief data={r.data} days={days} watchAll={watchAll} />;
}

export default function DistrictBriefPage({ params, searchParams }: Props) {
  return (
    <Suspense fallback={<DistrictSkeleton />}>
      <DistrictBody params={params} searchParams={searchParams} />
    </Suspense>
  );
}
