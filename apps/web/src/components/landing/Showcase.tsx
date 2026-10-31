import Link from 'next/link';
import { Suspense, type CSSProperties } from 'react';

import { Skeleton } from '@/components/ui/states';
import { absoluteApiUrl } from '@/lib/api';
import { dateIST, metricText, number, percent } from '@/lib/format';

import { getCases, getExamples, getHeadline } from './data';
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

// The slips' heights and tilts are composition (direction sheet: 26-44svh, two or three tilted).
const SLIP_SHAPE: { h: number; tilt: number }[] = [
  { h: 36, tilt: 0 },
  { h: 43, tilt: -1.4 },
  { h: 29, tilt: 0 },
  { h: 41, tilt: 1.2 },
  { h: 32, tilt: 0 },
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
  if (!day) return <div className={s.showcasePic} />;
  const pic = gibsPicture('viirsTrueColour', day, boxAround(AOI_CENTRE[0], AOI_CENTRE[1], 5.6, 1.6), 1920);
  return (
    <figure className={s.showcasePic}>
      <Pic src={pic.src} width={pic.width} height={pic.height} alt={`The plains of Punjab and Haryana from space on ${dateIST(day, { year: true })}, ${pic.sensor}.`} className={s.cover} failText="NASA GIBS did not send today’s picture of the plains." />
      <figcaption className={s.showcaseCaption}>{`The Punjab plains on ${dateIST(day)}, ${pic.sensor}. NASA GIBS.`}</figcaption>
    </figure>
  );
}

async function UnseenClaim() {
  const r = await getHeadline();
  const u = r.ok ? r.data.unseen_burns : null;
  if (!r.ok) return <p className={s.honestOnDark}>{`The burn scar totals did not load. ${r.error.message}`}</p>;
  if (!u?.available || u.unseen_share == null) {
    return <p className={s.honestOnDark}>{u?.reason ?? 'No scar has been checked against the fire alerts yet, so there is no unseen share to show.'}</p>;
  }
  return (
    <>
      <p className={s.claim}>
        <span className={s.claimFigure}>{percent(u.unseen_share)}</span>
        <span className={s.claimText}>
          {`of the burnt area Sentinel-2 mapped${u.first_pass ? ` since ${dateIST(u.first_pass)}` : ''} had no fire alert: ${metricText('area_ha', u.unseen_area_ha)} in ${number(u.unseen_clusters)} fields, the ${u.label ?? 'candidate unseen burns'}.`}
        </span>
      </p>
      <p className={s.claimAudit}>
        {u.precision != null
          ? `Audited precision ${percent(u.precision)}${u.precision_ci95 ? ` (95% CI ${percent(u.precision_ci95[0])}–${percent(u.precision_ci95[1])}` : ''}${u.precision_n != null ? `${u.precision_ci95 ? ', ' : ' ('}n = ${number(u.precision_n)})` : u.precision_ci95 ? ')' : ''}: some candidates are tilled or flooded fields, so treat the share as an upper bound.`
          : 'This share has not been audited yet: treat it as an upper bound.'}
      </p>
    </>
  );
}

async function Slips() {
  const { list, cases } = await getCases();
  if (!list.ok) return <p className={`${s.slipsMissing} ${s.honestOnDark}`}>{`The case files did not load. ${list.error.message}`}</p>;
  if (!cases.length) return <p className={`${s.slipsMissing} ${s.honestOnDark}`}>No candidate unseen burn has been confirmed against the fire alerts yet.</p>;
  return (
    <ol className={s.slips} aria-label="Case files of candidate unseen burns">
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
  const nearest = p.nearest_fire_m != null ? metricText('km', p.nearest_fire_m / 1000) : null;
  return (
    <Link href={`/case/${p.id}`} className={s.slip}>
      <span className={s.slipHead}>Case {p.id}</span>
      <span className={s.slipLabel}>{p.label ?? 'candidate unseen burn'}</span>
      <span className={s.slipRule} aria-hidden="true">
        - - - - - - - - -
      </span>
      <span>{p.village ? `Near ${p.village}${p.district ? `, ${p.district}` : ''}` : (p.district ?? 'Place not named')}</span>
      <span>{`${metricText('area_ha', p.area_ha)} burnt`}</span>
      <span>{`Between ${dateIST(p.burn_after)} and ${dateIST(p.burn_before)}`}</span>
      <span>{p.fire_matches === 0 ? `No fire alert near it${nearest ? `; nearest ${nearest}` : ''}` : `${number(p.fire_matches)} fire alerts near it`}</span>
      {sat ? <span>{`Seen by ${sat}`}</span> : null}
      {p.confidence != null ? <span>{`Confidence ${number(p.confidence, { decimals: 2 })}`}</span> : null}
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
    </Link>
  );
}

async function AnswerSlip() {
  const r = await getExamples();
  if (!r.ok) return null;
  const all = (r.data.examples ?? []).filter((e) => e.ok !== false && e.answer && e.question && e.verification?.passed);
  // The answer that speaks to what the satellites missed, if one is stored; else Delhi's air; else any.
  const pick: AgentExample | undefined =
    all.find((e) => /miss|unseen/i.test(e.question ?? '')) ?? all.find((e) => /delhi/i.test(e.question ?? '')) ?? all[0];
  if (!pick) return null;
  // One point, the one about the unobserved hours if the answer has it. (A scar total the investigator
  // scoped to its own question would sit beside the season total above and read as a contradiction.)
  const { lead, points } = answerParts(pick.answer, 1, /unobserved|missed/i);
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
        BlindSpot’s investigator{pick.model ? `, ${pick.model}` : ''}, <AnswerAge at={pick.finished_at ?? pick.started_at ?? null} />.
      </figcaption>
    </figure>
  );
}

function ClaimSkeleton() {
  return (
    <div aria-hidden="true" style={{ display: 'grid', gap: 10 }}>
      <Skeleton height={72} width="40%" />
      <Skeleton height={22} width="100%" />
      <Skeleton height={22} width="80%" />
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
