import Link from 'next/link';
import { Suspense } from 'react';

import { Skeleton } from '@/components/ui/states';
import { getBriefIndex } from '@/components/brief/data';
import { DEFAULT_DAYS, districtHref, windowWords } from '@/components/brief/read';
import { LoadFailed } from '@/components/pwa/LoadFailed';

/**
 * A district the API does not know (its 404 names the districts with burn scars). Rendered inside the
 * brief's frame, so the bar and the way back stay. The list comes from the API's 30-day brief, the
 * longest window it serves, so every district with a candidate unseen burn in it is one click away.
 */

const LIST_DAYS = 30;

async function KnownDistricts() {
  const r = await getBriefIndex(LIST_DAYS);
  // no list without the read; the mark keeps this render from replacing a good offline copy
  if (!r.ok) return <LoadFailed />;
  if (!r.data.districts?.length) return null;
  const byState = new Map<string, string[]>();
  for (const d of r.data.districts) {
    const s = d.state ?? 'Other';
    byState.set(s, [...(byState.get(s) ?? []), d.district]);
  }
  const w = r.data.window;
  return (
    <div className="br-known">
      {w ? <p className="br-known__caption">{`Districts with candidate unseen burns in ${windowWords(w)}`}</p> : null}
      {[...byState.entries()].map(([state, names]) => (
        <section key={state} className="br-known__state" aria-labelledby={`br-known-${state}`}>
          <h2 id={`br-known-${state}`} className="br-known__title">
            {state}
          </h2>
          <ul className="br-known__list">
            {names.sort((a, b) => a.localeCompare(b)).map((n) => (
              <li key={n}>
                <Link href={districtHref(n, DEFAULT_DAYS)} className="br-known__link">
                  {n}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export default function DistrictNotFound() {
  return (
    <>
      <div className="title-row not-found">
        <div className="title-row__text">
          <h1>No brief for this district</h1>
          <p className="finding">BlindSpot has no district by this name with burn scars in Punjab or Haryana. Check the spelling, or pick a district below.</p>
          <p className="not-found__links">
            <Link className="br-button br-button--primary" href="/brief">
              All districts, ranked
            </Link>
            <Link className="br-textlink" href="/live">
              Open the live map
            </Link>
          </p>
        </div>
      </div>
      <Suspense fallback={<Skeleton width="min(900px, 100%)" height={160} />}>
        <KnownDistricts />
      </Suspense>
    </>
  );
}
