import Link from 'next/link';
import { Suspense, type CSSProperties } from 'react';

import { LoadFailed, loadFailed } from '@/components/pwa/LoadFailed';
import { Skeleton } from '@/components/ui/states';
import { absoluteApiUrl } from '@/lib/api';
import { dateIST, metricText, number } from '@/lib/format';
import { modelName } from '@/lib/models';

import { getCases, getExamples, getHeadline, getSlipScars, UNSEEN_LIST_QUERY } from './data';
import { AOI_CENTRE, boxAround, gibsDay, gibsPicture } from './gibs';
import { s } from './styles';
import { Pic } from './Pic';
import { Reveal } from './Reveal';
import { AnswerAge } from './AnswerAge';
import type { AgentExample, ScarCase } from './types';
import { answerParts, order, sentinelName } from './words';

/**
 * Section 2, showcase (direction: "Output slips along the edge"). The plains from NASA GIBS on the
 * latest day full-bleed; the claim with its audit; then the output itself standing along the bottom
 * edge: six case files of candidate unseen burns (GET /api/scars?unseen=true, then each case), and one
 * verified investigator answer (GET /api/agent/examples).
 */

// The slips' heights and tilts are composition (direction sheet: 26-44svh, two or three tilted); the
// shortest still holds its picture and details above the pocket edge on a 900 px screen.
const SLIP_SHAPE: { h: number; tilt: number }[] = [
  { h: 36, tilt: 0 },
  { h: 43, tilt: -1.4 },
  { h: 34, tilt: 0 },
  { h: 41, tilt: 1.2 },
  { h: 35, tilt: 0 },
  { h: 39, tilt: -1 },
];

export function Showcase() {
  return (
    <section className={s.showcase} aria-labelledby="showcase-h">
      <Suspense fallback={<div className={s.showcasePic} />}>
        <PlainsPicture />
      </Suspense>
      <div className={s.showcaseTop}>
        <Reveal className={s.showcaseText}>
          <h2 id="showcase-h" className={s.showcaseTitle} data-r style={order(0)}>
            The scars keep the record
          </h2>
          <p className={s.showcaseLead} data-r style={order(1)}>
            A burnt field stays black on Sentinel-2 for days, whatever hour it was lit. We match every scar against every fire alert.
          </p>
          <div data-r style={order(2)}>
            <Suspense fallback={<ClaimSkeleton />}>
              <UnseenClaim />
            </Suspense>
          </div>
        </Reveal>
        <Suspense fallback={null}>
          <AnswerSlip />
        </Suspense>
      </div>
      <Reveal className={s.slipsWrap}>
        <Suspense fallback={<SlipsSkeleton />}>
          <Slips />
        </Suspense>
      </Reveal>
    </section>
  );
}

async function PlainsPicture() {
  const day = await gibsDay('viirsTrueColour');
  // GIBS did not answer: the empty frame, marked so the offline worker keeps its good copy
  if (!day) return <div className={s.showcasePic} data-load-failed="" />;
  const pic = gibsPicture('viirsTrueColour', day, boxAround(AOI_CENTRE[0], AOI_CENTRE[1], 5.6, 1.6), 1920);
  return (
    <figure className={s.showcasePic}>
      <Pic src={pic.src} width={pic.width} height={pic.height} alt={`The plains of Punjab and Haryana from space on ${dateIST(day, { year: true })}, ${pic.sensor}.`} className={s.cover} failText="NASA GIBS did not send today’s picture of the plains." />
      <figcaption className={s.showcaseCaption}>{`The Punjab plains on ${dateIST(day)}, ${pic.sensor}. NASA GIBS.`}</figcaption>
    </figure>
  );
}

/** The slips' scope in one line (the headline's share and its audit sit on the first screen). The
 *  slips come from the newest scars (the list is newest pass first), so they are the largest of the
 *  newest pass, not of the season: the line says which pass and how confident, from the list itself. */
async function UnseenClaim() {
  const [r, shown] = await Promise.all([getHeadline(), getSlipScars()]);
  const u = r.ok ? r.data.unseen_burns : null;
  if (!r.ok) return <p className={s.honestOnDark} data-load-failed="">{`The burn scar totals did not load. ${r.error.message}`}</p>;
  if (!u?.available || u.unseen_clusters == null) {
    return <p className={s.honestOnDark}>{u?.reason ?? 'No scar has been checked against the fire alerts yet.'}</p>;
  }
  const label = u.label ?? 'candidate unseen burns';
  const season = `${number(u.unseen_clusters)} ${label}${u.districts != null ? ` in ${number(u.districts)} districts` : ''}${u.first_pass ? ` since ${dateIST(u.first_pass)}` : ''}`;
  const passes = [...new Set(shown.map((p) => dateIST(p.burn_before)))];
  // (a list that did not load says so in the slips' own place; this line then gives the season only)
  const text = shown.length
    ? `The slips below are the largest ${label} of the ${passes.length === 1 ? `pass of ${passes[0]}` : 'newest passes'} with confidence ${number(UNSEEN_LIST_QUERY.min_confidence, { decimals: 1 })} or more, one per village: ${number(shown.length)} of the ${season}. Each opens its case file.`
    : `Sentinel-2 has mapped ${season}.`;
  return <p className={s.claimText}>{text}</p>;
}

async function Slips() {
  const { list, cases, failed } = await getCases();
  if (!list.ok) return <p className={`${s.slipsMissing} ${s.honestOnDark}`} data-load-failed="">{`The case files did not load. ${list.error.message}`}</p>;
  if (!cases.length) return <p className={`${s.slipsMissing} ${s.honestOnDark}`}>No candidate unseen burn has been confirmed against the fire alerts yet.</p>;
  // a case file that failed leaves its slip without a picture: the list carries the mark
  return (
    <ol className={s.slips} aria-label="Case files of candidate unseen burns" {...loadFailed(failed)}>
      {cases.map((c, n) => (
        <li key={c.case.id} className={s.slipItem} data-r style={{ '--h': `${SLIP_SHAPE[n % SLIP_SHAPE.length].h}svh`, '--tilt': `${SLIP_SHAPE[n % SLIP_SHAPE.length].tilt}deg`, '--i': n } as CSSProperties}>
          <CaseSlip c={c} />
        </li>
      ))}
    </ol>
  );
}

function CaseSlip({ c }: { c: ScarCase }) {
  const p = c.case;
  const post = c.imagery?.post;
  const chip = absoluteApiUrl(post?.truecolor_url);
  const sat = sentinelName(post?.item_id ?? p.scene_post);
  const nearest = p.nearest_fire_m != null ? metricText('near_km', p.nearest_fire_m / 1000) : null;
  return (
    <Link href={`/case/${p.id}`} className={s.slip}>
      <span className={s.slipHead}>Case {p.id}</span>
      <span className={s.slipLabel}>{p.label ?? 'candidate unseen burn'}</span>
      {chip && post ? (
        <Pic
          src={chip}
          width={512}
          height={512}
          alt={`Sentinel-2 true colour of case ${p.id} on ${dateIST(post.date)}: the burnt field is the black patch in the middle.`}
          className={s.cover}
          frame="lp-box-chip"
          failText="The Sentinel-2 crop did not load."
        />
      ) : null}
      <span className={s.slipRule} aria-hidden="true">
        - - - - - - - - -
      </span>
      <span>{p.village ? `Near ${p.village}${p.district ? `, ${p.district}` : ''}` : (p.district ?? 'Place not named')}</span>
      <span>{`${metricText('area_ha', p.area_ha)} burnt`}</span>
      <span>{`Between ${dateIST(p.burn_after)} and ${dateIST(p.burn_before)}`}</span>
      <span>{p.fire_matches === 0 ? `No fire alert near it${nearest ? `; nearest ${nearest}` : ''}` : `${number(p.fire_matches)} fire alerts near it`}</span>
      {sat ? <span>{`Seen by ${sat}`}</span> : null}
      {p.confidence != null ? <span>{`Confidence ${number(p.confidence, { decimals: 2 })}`}</span> : null}
    </Link>
  );
}

async function AnswerSlip() {
  const [r, slips] = await Promise.all([getExamples(), getSlipScars()]);
  // no slip without the read, but the render is marked, so it never replaces a good offline copy
  if (!r.ok) return <LoadFailed />;
  const all = (r.data.examples ?? []).filter((e) => e.ok !== false && e.answer && e.question && e.verification?.passed);
  // The answer about one of the case files below, if one is stored. An answer about any other case is
  // left out: it may be weeks old and about a scar the season's rebuild has replaced. Else the daily
  // job's featured answers (new each morning): what the satellites missed, Delhi's air, any of them.
  const caseOf = (e: AgentExample) => /\bcase (\d+)/i.exec(e.question ?? '')?.[1];
  const onSlips = new Set(slips.map((p) => String(p.id)));
  const general = all.filter((e) => caseOf(e) == null);
  const featured = general.filter((e) => e.kind === 'featured');
  const pool = featured.length ? featured : general;
  const pick: AgentExample | undefined =
    all.find((e) => onSlips.has(caseOf(e) ?? '')) ??
    pool.find((e) => /miss|unseen/i.test(e.question ?? '')) ??
    pool.find((e) => /delhi/i.test(e.question ?? '')) ??
    pool[0];
  if (!pick) return null;
  // One point. Left out: any point naming an unobserved span (the investigator measures its own gap
  // between FIRMS detections, which would sit beside the clock's polar-window span as a second,
  // different figure) and any hectare total (scoped to its own question, it would read against the
  // season total on the first screen). Preferred: a point about the missing fire alert.
  const { lead, points } = answerParts(pick.answer, 1, /zero|no (?:active )?fire|not detected|missed/i, /unobserved|\bgap\b|\d\s*ha\b/i);
  const v = pick.verification;
  return (
    <figure className={s.answer}>
      <span className={s.slipHead}>{pick.question}</span>
      <span className={s.slipRule} aria-hidden="true">
        - - - - - - - - - - - -
      </span>
      <blockquote className={s.answerText}>
        {lead ? <p>{lead}</p> : null}
        {points.length ? (
          <ul className={s.answerPoints}>
            {points.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        ) : null}
      </blockquote>
      <figcaption className={s.answerMeta}>
        {v?.checked != null && v.verified != null ? `${number(v.verified)} of ${number(v.checked)} figures checked against the data. ` : ''}
        BlindSpot’s investigator{pick.model ? `, ${modelName(pick.model)}` : ''}, <AnswerAge at={pick.finished_at ?? pick.started_at ?? null} />.
      </figcaption>
    </figure>
  );
}

function ClaimSkeleton() {
  return (
    <div aria-hidden="true" style={{ display: 'grid', gap: 10 }}>
      <Skeleton height={22} width="100%" />
      <Skeleton height={22} width="70%" />
    </div>
  );
}

function SlipsSkeleton() {
  return (
    <ol className={s.slips} aria-hidden="true">
      {SLIP_SHAPE.map((sh, n) => (
        <li key={n} className={s.slipItem} style={{ '--h': `${sh.h}svh`, '--tilt': '0deg' } as CSSProperties}>
          <span className={`${s.slip} ${s.slipEmpty}`} />
        </li>
      ))}
    </ol>
  );
}
