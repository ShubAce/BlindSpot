import Link from 'next/link';
import { Suspense } from 'react';

import { Skeleton } from '@/components/ui/states';

import { getHeadline } from './data';
import { DelhiAir } from './DelhiAir';
import { s } from './styles';
import { Reveal } from './Reveal';
import { order } from './words';

/** Section 6, call to action (direction: "The reply card"). */
export function Reply() {
  return (
    <Reveal as="section" className={s.reply} aria-labelledby="reply-h">
      <div className={s.replyLeft} data-r style={order(0)}>
        <Suspense fallback={<AirSkeleton />}>
          <Air />
        </Suspense>
      </div>
      <div className={s.replyCard} data-r style={order(1)}>
        <h2 id="reply-h" className={s.replyTitle}>
          Ask why
        </h2>
        <p className={s.replyLines}>Ask whether today’s smoke came from Punjab. The investigator checks the wind, the fires and the scars on the live map, and says what the data cannot settle.</p>
        <Link href="/live" className={s.replyBar} transitionTypes={['to-live']}>
          Open mission control
        </Link>
      </div>
    </Reveal>
  );
}

async function Air() {
  const r = await getHeadline();
  return <DelhiAir initial={r.ok ? r.data : null} />;
}

function AirSkeleton() {
  return (
    <div aria-hidden="true" style={{ display: 'grid', gap: 10 }}>
      {[100, 96, 100, 70].map((w, i) => (
        <Skeleton key={i} height={24} width={`${w}%`} />
      ))}
    </div>
  );
}
