import type { Metadata } from 'next';
import { notFound, permanentRedirect } from 'next/navigation';
import { Suspense } from 'react';

import { CaseScreen } from '@/components/case/CaseScreen';
import { CaseSkeleton } from '@/components/case/CaseSkeleton';
import type { CaseFile } from '@/components/case/types';
import { caseState } from '@/components/case/audit';
import { ScreenError } from '@/components/case/ScreenError';
import { PATHS } from '@/lib/api';
import { serverGet } from '@/lib/api-server';
import { dateIST, metricText } from '@/lib/format';

/**
 * /case/[id]: the case file of one Sentinel-2 burn scar. The shell (bar, frame and a skeleton of the
 * real layout) is prerendered; the case streams in when the API answers (lib/api-server.ts caches a
 * good answer for 30 s). An id the API does not know renders this segment's not-found page. An id the
 * sentinel job replaced (a rebuild or a district mapped at another resolution gave the same burn a new
 * outline and id) comes back as its successor's case with `forwarded_from`, and the old link redirects
 * permanently to /case/<new id> (a 308 where the response has not started; inside the streamed body
 * Next.js redirects with a meta tag).
 */

type Props = { params: Promise<{ id: string }> };

const isId = (id: string) => /^\d{1,12}$/.test(id);

const getCase = (id: string) => serverGet<CaseFile>(PATHS.scarCase(id), undefined, 12_000);

/** The case's own address when the API answered for a replaced id (`forwarded_from`), else null. */
const movedTo = (data: CaseFile) => (data.forwarded_from != null ? `/case/${data.case.id}` : null);

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  if (!isId(id)) return { title: 'Case not found', robots: { index: false } };
  const r = await getCase(id);
  if (!r.ok) return { title: r.error.status === 404 ? 'Case not found' : `Case ${id}` };
  const moved = movedTo(r.data);
  if (moved) permanentRedirect(moved);
  const c = r.data.case;
  const where = [c.village ? `near ${c.village}` : null, c.district].filter(Boolean).join(', ');
  const st = caseState(c.unseen);
  const what = st === 'unseen' ? 'Candidate unseen burn' : st === 'alerted' ? 'Burn a fire satellite saw' : 'Burn not yet checked against fire alerts';
  return {
    title: `Burn scar ${where ? `${where}, ` : ''}${dateIST(c.burn_before, { year: true })} (case ${c.id})`,
    description: `${what}: ${metricText('area_ha', c.area_ha)} ${where ? `${where} ` : ''}between the Sentinel-2 passes of ${dateIST(c.burn_after)} and ${dateIST(c.burn_before, { year: true })}.`,
    alternates: { canonical: `/case/${c.id}` },
  };
}

async function CaseBody({ params }: Props) {
  const { id } = await params;
  if (!isId(id)) notFound();
  const r = await getCase(id);
  if (!r.ok) {
    if (r.error.status === 404 || r.error.status === 422) notFound();
    return <ScreenError title={`Case ${id}`} what="this case file" message={r.error.message} />;
  }
  const moved = movedTo(r.data);
  if (moved) permanentRedirect(moved);
  return <CaseScreen data={r.data} />;
}

export default function CasePage({ params }: Props) {
  return (
    <Suspense fallback={<CaseSkeleton />}>
      <CaseBody params={params} />
    </Suspense>
  );
}
