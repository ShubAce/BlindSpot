import { Fragment, Suspense } from 'react';

import { Skeleton } from '@/components/ui/states';
import { PATHS, type SourcesResponse } from '@/lib/api';
import { serverGet } from '@/lib/api-server';
import { dateIST } from '@/lib/format';

import { AOI_CENTRE, boxAround, gibsDay, gibsPicture } from './gibs';
import { s } from './styles';
import { Pic } from './Pic';
import { Reveal } from './Reveal';
import { order } from './words';
import { SourcesLine } from './SourcesLine';

/**
 * Section 5, proof (direction: "The printed guest list"). The names on the card are the sources the
 * API reports on right now (GET /api/sources), each linking to its documentation; the line under the
 * card says, live, how many of them have answered on schedule.
 */
export function Proof() {
  return (
    <Reveal as="section" className={s.proof} id="sources" aria-labelledby="proof-h">
      <div className={s.card} data-r style={order(0)}>
        <h2 id="proof-h" className={s.cardTitle}>
          Where it comes from
        </h2>
        <Suspense fallback={<div className={`lp-box lp-box-wide ${s.tipIn}`} />}>
          <TippedIn />
        </Suspense>
        <Suspense fallback={<NamesSkeleton />}>
          <Names />
        </Suspense>
      </div>
      <div className={s.proofStatus} data-r style={order(1)}>
        <Suspense fallback={<Skeleton width="28ch" height={20} />}>
          <Status />
        </Suspense>
      </div>
    </Reveal>
  );
}

async function TippedIn() {
  const day = await gibsDay('modisFires');
  if (!day) return <div className={`lp-box lp-box-wide ${s.tipIn}`} />;
  const pic = gibsPicture('modisFires', day, boxAround(AOI_CENTRE[0], AOI_CENTRE[1], 6, 16 / 9), 1280);
  return (
    <figure className={s.tipIn}>
      <Pic src={pic.src} width={pic.width} height={pic.height} alt={`Fires and haze over Punjab and Haryana on ${dateIST(day, { year: true })}, ${pic.sensor}.`} className={s.cover} frame="lp-box-wide" failText="NASA GIBS did not send this picture." />
      <figcaption className={s.cardCaption}>{`Punjab and Haryana, ${dateIST(day)}: ${pic.sensor}. NASA GIBS.`}</figcaption>
    </figure>
  );
}

async function Names() {
  const r = await serverGet<SourcesResponse>(PATHS.sources);
  if (!r.ok) return <p className={s.cardHonest}>{`The list of sources did not load. ${r.error.message}`}</p>;
  // A source whose label is only its key is internal plumbing (a cache, a mirror), not a name.
  const named = r.data.sources.filter((x) => x.label && x.label !== x.key);
  return (
    <p className={s.names}>
      {named.map((x, i) => (
        <Fragment key={x.key}>
          {x.docs_url ? (
            <a href={x.docs_url} className={s.nameLink} rel="noreferrer" target="_blank">
              {x.label}
            </a>
          ) : (
            <span>{x.label}</span>
          )}
          {i < named.length - 1 ? <span aria-hidden="true">{' · '} </span> : null}
        </Fragment>
      ))}
    </p>
  );
}

async function Status() {
  const r = await serverGet<SourcesResponse>(PATHS.sources);
  return <SourcesLine initial={r.ok ? r.data : null} />;
}

function NamesSkeleton() {
  return (
    <div aria-hidden="true" style={{ display: 'grid', gap: 14, justifyItems: 'center' }}>
      {[92, 80, 96, 70, 88, 60].map((w, i) => (
        <Skeleton key={i} height={22} width={`${w}%`} />
      ))}
    </div>
  );
}
