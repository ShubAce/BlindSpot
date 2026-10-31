import { Suspense } from 'react';

import { Skeleton } from '@/components/ui/states';
import { REFERENCE_SITE } from '@/lib/aoi';
import { dateIST, number, percent, timeIST } from '@/lib/format';
import { PHOTOS } from '@/lib/photo-credits';

import { getHeadline, getPasses } from './data';
import { s } from './styles';
import { Pic } from './Pic';
import { Reveal } from './Reveal';
import { order, span } from './words';

/**
 * Section 1, intro: the clock's two readings in words and the timetable behind its live line. The
 * statement and its body come from GET /api/stats/headline (GK-2A's evening block, measured against
 * the same polar windows the clock draws in gold, and the day's FIRMS count); the timetable is today's
 * passes over the reference site from GET /api/orbits/passes, the list the clock's live line counts
 * down. A large photograph of the field work holds the left half.
 */
export function Intro() {
  return (
    <Reveal as="section" className={s.intro} aria-labelledby="intro-h">
      <h2 id="intro-h" className="sr-only">
        When the satellites look, and when the fields burn
      </h2>
      <figure className={s.introPhoto} data-r style={order(1)}>
        <Pic
          src={PHOTOS.rakingStubble.src}
          width={PHOTOS.rakingStubble.width}
          height={PHOTOS.rakingStubble.height}
          alt="Farmers raking paddy stubble in a hazy field in Punjab."
          className={s.cover}
          sizes="(max-width: 760px) 100vw, 40vw"
          failText="This photograph did not load."
          frame="lp-box-intro"
          local
        />
        <figcaption className={s.caption}>Raking paddy stubble before a burn, Punjab. Photograph: CIAT.</figcaption>
      </figure>
      <div className={s.introText} data-r style={order(0)}>
        <Suspense fallback={<StatementSkeleton />}>
          <IntroWords />
        </Suspense>
      </div>
      <div className={s.introTable} data-r style={order(2)}>
        <Suspense fallback={<Skeleton height={320} />}>
          <Timetable />
        </Suspense>
      </div>
    </Reveal>
  );
}

async function IntroWords() {
  const r = await getHeadline();
  if (!r.ok) return <p className={s.honest}>{`Today’s counts did not load. ${r.error.message}`}</p>;
  const geo = r.data.gk2a_evening?.available ? r.data.gk2a_evening : null;
  const fires = r.data.fires_24h?.available ? r.data.fires_24h : null;
  const region = fires?.region ? fires.region.replace(/\s*\(.*\)\s*$/, '').replace(/, ([^,]+)$/, ' and $1') : null;
  return (
    <>
      <p className={s.statement}>
        {geo?.share_outside_polar_windows != null
          ? `${percent(geo.share_outside_polar_windows)} of the fire activity GK-2A saw in the last ${number(geo.days)} days fell outside the polar satellites’ looks.`
          : 'GK-2A’s record of the day’s fire activity has not loaded, so the share outside the polar satellites’ looks cannot be given.'}
      </p>
      <p className={s.introBody}>
        {geo ? `GK-2A is a geostationary satellite that scans Punjab every ten minutes${geo.peak_ist ? `; the activity it sees peaks at ${span(geo.peak_ist)} IST` : ''}. Its figure is an activity index, not a count of fires. ` : ''}
        {fires?.count != null
          ? `The polar satellites’ count is NASA FIRMS: ${number(fires.count)} fire detections in the 24 hours to ${fires.window_ist?.to ? fires.window_ist.to.slice(11, 16) : timeIST(r.data.as_of)} IST${region ? ` in ${region}` : ''}.`
          : ''}
      </p>
    </>
  );
}

async function Timetable() {
  const r = await getPasses();
  if (!r.ok) return <p className={s.honest}>{`Today’s pass list did not load. ${r.error.message}`}</p>;
  const at = r.data.generated_at;
  const day = dateIST(at);
  const today = (r.data.passes ?? []).filter((p) => dateIST(p.start_utc) === day || dateIST(p.end_utc) === day);
  if (!today.length) return <p className={s.honest}>No fire-sensor pass over {REFERENCE_SITE.name} falls on this IST day in the pass list.</p>;
  const now = Date.parse(at);
  const minutes = (p: (typeof today)[number]) => Math.max(1, Math.round((Date.parse(p.end_utc) - Date.parse(p.start_utc)) / 60_000));
  return (
    <table className={s.passTable}>
      <caption className={s.passCaption}>{`Today’s fire-satellite passes over ${REFERENCE_SITE.name}, ${day}, IST, the list the clock’s live line counts down; paler rows are still to come`}</caption>
      <thead>
        <tr>
          <th scope="col">In view</th>
          <th scope="col">Satellite</th>
          <th scope="col">Sensor</th>
          <th scope="col" className={s.passNum}>
            Minutes
          </th>
        </tr>
      </thead>
      <tbody>
        {today.map((p) => (
          <tr key={p.start_utc + p.satellite} data-done={Date.parse(p.end_utc) <= now ? 'yes' : 'no'}>
            <td>{`${timeIST(p.start_utc)}–${timeIST(p.end_utc)}`}</td>
            <td>{p.satellite}</td>
            <td>{p.instrument}</td>
            <td className={s.passNum}>{number(minutes(p))}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StatementSkeleton() {
  return (
    <div aria-hidden="true" className={s.statementSkeleton}>
      <Skeleton height="0.9em" width="96%" />
      <Skeleton height="0.9em" width="100%" />
      <Skeleton height="0.9em" width="60%" />
    </div>
  );
}
