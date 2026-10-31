import { Suspense } from 'react';

import { Skeleton } from '@/components/ui/states';
import { PHOTOS } from '@/lib/photo-credits';

import { getHeadline } from './data';
import { IntroLive } from './IntroLive';
import { s } from './styles';
import { Pic } from './Pic';
import { Reveal } from './Reveal';
import { order } from './words';

/**
 * Section 1, intro (direction: "Right-half paragraph, lone photo"). No heading on the page: the
 * statement is the section, so it carries an off-screen h2 for the outline.
 */
export function Intro() {
  return (
    <Reveal as="section" className={s.intro} aria-labelledby="intro-h">
      <h2 id="intro-h" className="sr-only">
        Who is watching Punjab now
      </h2>
      <div className={s.introText} data-r style={order(0)}>
        <Suspense fallback={<StatementSkeleton />}>
          <IntroData />
        </Suspense>
      </div>
      <figure className={s.introPhoto} data-r style={order(1)}>
        <Pic
          src={PHOTOS.rakingStubble.src}
          width={PHOTOS.rakingStubble.width}
          height={PHOTOS.rakingStubble.height}
          alt="Farmers raking paddy stubble in a hazy field in Punjab."
          className={s.cover}
          sizes="(max-width: 760px) 62vw, 20vw"
          failText="This photograph did not load."
          frame="lp-box-intro"
          local
        />
        <figcaption className={s.caption}>Raking paddy stubble before a burn, Punjab. Photograph: CIAT.</figcaption>
      </figure>
      <p className={s.introNote} data-r style={order(2)}>
        Fire detections: NASA FIRMS. Pass times: computed in your browser from CelesTrak orbital elements. Evening activity: GK-2A, an activity index in pixel-hours, not a fire count.
      </p>
    </Reveal>
  );
}

async function IntroData() {
  const r = await getHeadline();
  // The server's failure is passed down: the browser keeps polling and drops the line once data arrives.
  return <IntroLive initial={r.ok ? r.data : null} serverError={r.ok ? null : r.error.message} />;
}

function StatementSkeleton() {
  return (
    <div aria-hidden="true" className={s.statementSkeleton}>
      <Skeleton height="0.9em" width="96%" />
      <Skeleton height="0.9em" width="100%" />
      <Skeleton height="0.9em" width="92%" />
      <Skeleton height="0.9em" width="98%" />
      <Skeleton height="0.9em" width="60%" />
    </div>
  );
}
