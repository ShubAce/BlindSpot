import Link from 'next/link';
import { Suspense, type CSSProperties, type ReactNode } from 'react';

import { Skeleton } from '@/components/ui/states';
import { absoluteApiUrl } from '@/lib/api';
import { dateIST, metricText, number, percent, timeIST } from '@/lib/format';
import { PHOTOS } from '@/lib/photo-credits';

import { getCases, getDelhiMet, getFireHours, getHeadline } from './data';
import { DragRow } from './DragRow';
import { AOI_CENTRE, boxAround, gibsDay, gibsPicture } from './gibs';
import { s } from './styles';
import { Pic } from './Pic';
import { Reveal } from './Reveal';
import type { FireWindow } from './types';
import { span } from './words';

/**
 * Section 4, how it works (direction: "Drag row of moments"): one day over Punjab in eight moments,
 * each with a real picture. The times, counts and heights in the captions are live (fire windows from
 * GET /api/fires/hours, the evening peak from the headline's GK-2A block, Delhi's mixing layer from
 * GET /api/met/now); the satellite pictures are NASA GIBS on their latest day and the API's own
 * Sentinel-2 crops of a real case.
 */

type Picture = { src: string; width: number; height: number; alt: string; caption: string; local?: boolean; fail: string };
type Moment = { key: string; title: string; text: ReactNode; pic: Picture | null; href?: string };

// The empty space above each picture varies so the pictures' tops rise and fall (direction sheet).
const GAPS = [96, 132, 80, 124, 104, 140, 88, 116];

export function OneDay() {
  return (
    <Reveal as="section" className={s.day} aria-labelledby="day-h">
      <Suspense fallback={<DaySkeleton />}>
        <Moments />
      </Suspense>
    </Reveal>
  );
}

/** The main daytime and night observation windows from the week's detections. */
function mainWindow(ws: FireWindow[] | undefined, dn: 'D' | 'N'): FireWindow | null {
  return (ws ?? []).filter((w) => w.daynight === dn && !w.minor).sort((a, b) => (b.share ?? 0) - (a.share ?? 0))[0] ?? null;
}

async function Moments() {
  const [hours, head, met, cases, dayFires, dayDrift, dayNight] = await Promise.all([
    getFireHours(),
    getHeadline(),
    getDelhiMet(),
    getCases(),
    gibsDay('viirsFires'),
    gibsDay('viirsTrueColour'),
    gibsDay('nightBand'),
  ]);
  const ws = hours.ok ? hours.data.observed_windows_ist : undefined;
  const day = mainWindow(ws, 'D');
  const night = mainWindow(ws, 'N');
  const geo = head.ok && head.data.gk2a_evening?.available ? head.data.gk2a_evening : null;
  const blind = geo?.unobserved_span_ist ?? null;
  const blh = met.ok ? met.data.blh_m : null;
  const c0 = cases.cases[0];
  const c1 = cases.cases[1] ?? cases.cases[0];
  const portrait = (lonW: number) => boxAround(AOI_CENTRE[0], AOI_CENTRE[1], lonW, 4 / 5);

  const gibs = (key: 'viirsFires' | 'viirsTrueColour' | 'nightBand', d: string | null, lonW: number, what: string): Picture | null => {
    if (!d) return null;
    const p = gibsPicture(key, d, portrait(lonW), 560);
    return { src: p.src, width: p.width, height: p.height, alt: `${what} on ${dateIST(d, { year: true })}, ${p.sensor}.`, caption: `${what}, ${dateIST(d)}. ${p.sensor}, NASA GIBS.`, fail: 'NASA GIBS did not send this picture.' };
  };
  const chip = (url: string | null | undefined, id: number, date: string | null | undefined, what: string): Picture | null => {
    const src = absoluteApiUrl(url);
    if (!src) return null;
    return { src, width: 512, height: 512, alt: `Case ${id} on ${dateIST(date, { year: true })}, ${what}. The burnt field is the dark patch in the middle.`, caption: `Case ${id}, ${dateIST(date)}, ${what}. Sentinel-2.`, fail: 'The Sentinel-2 crop did not load from the API.' };
  };
  const photo = (k: keyof typeof PHOTOS, alt: string, caption: string): Picture => ({ src: PHOTOS[k].src, width: PHOTOS[k].width, height: PHOTOS[k].height, alt, caption, local: true, fail: 'This photograph did not load.' });

  const moments: Moment[] = [
    {
      key: 'pass',
      title: 'The afternoon pass',
      text: day
        ? `${number(day.satellites?.length ?? 0)} fire satellites cross Punjab between ${day.start} and ${day.end} IST and count what is burning.`
        : 'The fire satellites cross Punjab in the early afternoon and count what is burning.',
      pic: gibs('viirsFires', dayFires, 3.2, 'Punjab at the afternoon pass'),
    },
    {
      key: 'peak',
      title: 'The evening peak',
      // the same blind span as the clock on the first screen (the headline's GK-2A block), not FIRMS's own gap
      text: `${geo?.peak_ist ? `GK-2A’s ten-minute scans put the peak of fire activity at ${span(geo.peak_ist)} IST.` : 'Burning goes on after the afternoon pass.'}${blind ? ` In the last ${geo?.days != null ? `${number(geo.days)} days` : 'days'} no polar satellite reported a fire in Punjab or Haryana from ${blind.start} to ${blind.end} IST${blind.pixel_hours != null && geo?.pixel_hours ? `, the span that held ${percent(blind.pixel_hours / geo.pixel_hours)} of that activity` : ''}.` : ''}`,
      pic: photo('strawBurning', 'Farmers turning burning rice straw in a field in Punjab, smoke behind them.', 'Rice straw burning, Punjab. Photograph: CIAT.'),
    },
    {
      key: 'drift',
      title: 'The night drift',
      text: 'Smoke rides the evening wind towards Delhi. Our particle model follows it hour by hour on two weather models and shows where they disagree.',
      pic: gibs('viirsTrueColour', dayDrift, 6.4, 'The plains from Punjab to Delhi'),
    },
    {
      key: 'lid',
      title: 'Delhi under a lid',
      text:
        blh != null && met.ok && met.data.t
          ? `At ${timeIST(met.data.t)} IST the mixing layer over Delhi was ${metricText('blh_m', blh)} deep. What arrives stays near the ground.`
          : 'At night the mixing layer over Delhi sinks, and what arrives stays near the ground.',
      pic: photo('delhiSmog', 'A platform at New Delhi railway station fading into smog on a winter morning.', 'Smog at New Delhi railway station, a winter morning. Photograph: Sumita Roy Dutta.'),
    },
    {
      key: 'night',
      title: 'The night pass',
      text: night
        ? `Between ${night.start} and ${night.end} IST the satellites pass again and see embers, not the fields already burnt.`
        : 'In the small hours the satellites pass again and see embers, not the fields already burnt.',
      pic: gibs('nightBand', dayNight, 4.2, 'Punjab at night'),
    },
    {
      key: 'scar',
      title: 'Days later',
      text: 'Sentinel-2 photographs the fields again. The scar is there, alert or no alert.',
      pic: c0 ? chip(c0.imagery?.post?.swir_url, c0.case.id, c0.imagery?.post?.date, 'short-wave infrared') : null,
    },
    {
      key: 'case',
      title: 'The case file',
      text: 'Each candidate unseen burn becomes a record: where, how big, between which two passes.',
      pic: c1 ? chip(c1.imagery?.post?.truecolor_url, c1.case.id, c1.imagery?.post?.date, 'true colour') : null,
      href: c1 ? `/case/${c1.case.id}` : undefined,
    },
    {
      key: 'help',
      title: 'Help, not blame',
      text: 'Fields cut but not burnt rise to the top of the list, so a seeder can reach them first.',
      pic: photo('happySeeder', 'A zero-till seeder hitched to a tractor on a field of rice stubble.', 'A zero-till seeder, Punjab. Photograph: FatherofGod.'),
    },
  ];

  const h = hours.ok ? hours.data : null;
  const aside =
    h?.daytime_share != null && h.total != null && h.days
      ? `Of the ${number(h.total)} fire detections of the last ${number(h.days)} days, ${percent(h.daytime_share)} were made on daytime passes.`
      : null;

  return (
    <DragRow title="One day over Punjab" titleId="day-h" aside={aside}>
      {moments.map((m, i) => (
        <li key={m.key} className={s.moment} style={{ '--gap': `${GAPS[i % GAPS.length]}px` } as CSSProperties}>
          <h3 className={s.momentTitle}>{m.title}</h3>
          <p className={s.momentText}>{m.text}</p>
          <figure className={s.momentPic}>
            {m.pic ? (
              <>
                {m.href ? (
                  <Link href={m.href} className={s.momentLink} aria-label={`Open ${m.pic.caption.replace(/\.$/, '')}`}>
                    <Pic src={m.pic.src} width={m.pic.width} height={m.pic.height} alt={m.pic.alt} className={s.cover} sizes="(max-width: 760px) 72vw, 280px" failText={m.pic.fail} local={m.pic.local} frame="lp-box-portrait" />
                  </Link>
                ) : (
                  <Pic src={m.pic.src} width={m.pic.width} height={m.pic.height} alt={m.pic.alt} className={s.cover} sizes="(max-width: 760px) 72vw, 280px" failText={m.pic.fail} local={m.pic.local} frame="lp-box-portrait" />
                )}
                <figcaption className={s.caption}>{m.pic.caption}</figcaption>
              </>
            ) : (
              <span className={s.momentMissing}>No picture for this moment right now: its source did not answer.</span>
            )}
          </figure>
        </li>
      ))}
    </DragRow>
  );
}

function DaySkeleton() {
  return (
    <div aria-hidden="true">
      <h2 className={s.dayTitle}>One day over Punjab</h2>
      <div className={s.dayBar} />
      <div className={s.dayRow}>
        {GAPS.slice(0, 5).map((g, i) => (
          <div key={i} className={s.moment} style={{ '--gap': `${g}px` } as CSSProperties}>
            <Skeleton height={22} width="70%" />
            <Skeleton height={60} width="100%" style={{ marginTop: 12 }} />
            <Skeleton height={350} width="100%" style={{ marginTop: g }} />
          </div>
        ))}
      </div>
    </div>
  );
}
