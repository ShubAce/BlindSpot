import type { Metadata } from 'next';
import { Suspense } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';
import { auditPhrase } from '@/components/case/audit';
import type { HeadlineAudit } from '@/components/case/types';
import { BulletinReader } from '@/components/bulletin/BulletinReader';
import { BulletinSkeleton } from '@/components/bulletin/BulletinSkeleton';
import type { Bulletin, BulletinLangCode, BulletinSet } from '@/components/bulletin/types';
import { LoadFailed, loadFailed } from '@/components/pwa/LoadFailed';
import { PATHS } from '@/lib/api';
import { serverGet } from '@/lib/api-server';

import '@/components/case/app-screen.css';
import '@/components/bulletin/bulletin.css';

/**
 * /bulletin: today's bulletin, written by the investigator from the live data and checked number by
 * number. The three languages are read on the server in parallel (each cached 30 s by serverGet) so
 * the switch is instant and a flagged language is known before anything is shown.
 */

export const metadata: Metadata = {
  title: 'Daily bulletin',
  description: "Today's BlindSpot bulletin on Punjab's crop fires and Delhi's air, in English, Hindi and Punjabi, with every number checked against its source.",
};

const CODES: BulletinLangCode[] = ['en', 'hi', 'pa'];

async function AuditNote() {
  const r = await serverGet<HeadlineAudit>(PATHS.headline);
  const phrase = r.ok ? auditPhrase(r.data.unseen_burns, 'was') : null;
  return (
    <p className="card__note reading" {...loadFailed(!r.ok)}>
      Candidate unseen burns are Sentinel-2 burn scars with no FIRMS fire alert near them.
      {phrase ? ` When people checked a random sample by eye, the ${phrase}.` : r.ok ? ' The API states no audit for them yet.' : ` Their audit figures could not be loaded: ${r.error.message}`} Times are India Standard Time.
    </p>
  );
}

async function BulletinBody({ searchParams }: { searchParams: Promise<{ lang?: string | string[] }> }) {
  const sp = await searchParams;
  const asked = Array.isArray(sp.lang) ? sp.lang[0] : sp.lang;
  const initial: BulletinLangCode = CODES.includes(asked as BulletinLangCode) ? (asked as BulletinLangCode) : 'en';
  const results = await Promise.all(CODES.map((lang) => serverGet<Bulletin>(PATHS.bulletinLatest, { lang })));
  const set = Object.fromEntries(CODES.map((c, i) => [c, results[i]])) as BulletinSet;
  // a language the API failed to serve (a 404 is an answer: no bulletin today); the reader says so
  const failed = results.some((r) => !r.ok && r.error.status !== 404);
  return (
    <>
      <BulletinReader
        set={set}
        initial={initial}
        auditNote={
          <Suspense fallback={<span className="skeleton" style={{ height: 60 }} />}>
            <AuditNote />
          </Suspense>
        }
      />
      {failed ? <LoadFailed /> : null}
    </>
  );
}

export default function BulletinPage({ searchParams }: { searchParams: Promise<{ lang?: string | string[] }> }) {
  return (
    <div data-surface="app" className="app-screen bulletin-screen">
      <SiteNav current="bulletin" skipLabel="Skip to the bulletin" />

      <main id="main" className="app-canvas bulletin-canvas">
        <Suspense fallback={<BulletinSkeleton />}>
          <BulletinBody searchParams={searchParams} />
        </Suspense>
      </main>
    </div>
  );
}
