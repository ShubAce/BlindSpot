'use client';

import { dateIST, number, percent, timeIST } from '@/lib/format';
import { useNow } from '@/lib/live';

import { angleOf, arc, clockSpan, clockTime, DIAL, inGap, polar, sector, type ClockModel, type LivePass } from './clock';
import { s } from './styles';
import { countdown, span } from './words';

/**
 * The blind-spot clock: a 24-hour IST dial drawn from the API (see clock.ts). Gold marks are the
 * times of day a polar fire satellite reported a fire in Punjab or Haryana in the last days (the API
 * builds them from FIRMS detections), the red bars GK-2A's fire activity by hour over the same days,
 * the bite in the ring the span between reports that holds the most of that activity (the API's own
 * blind spot), and the white hand now.
 *
 * GK-2A watches all day, so the dial never says nobody looks. And a polar satellite can pass without
 * reporting anything (Terra crosses at night), so it never says none looked either: the bite is where
 * no polar satellite reported a fire, and the red bars are what the geostationary satellite saw.
 *
 * The whole dial is in the server HTML, drawn at the moment the pass list was computed, so it reads
 * without script. In the browser only the hand and the live line move: `useNow` is null during the
 * server render and hydration, so both start from the data's own timestamp and then follow the clock.
 * The live line counts down to the next computed pass over the reference site.
 */

export type ClockMeta = {
  site: string;
  /** passes' generated_at (ms): "now" until the browser clock runs */
  at: number;
  days: number | null;
  /** GK-2A activity outside the gold windows (the API measures it against exactly these windows) */
  share: number | null;
  peak: string | null;
  diurnalError: string | null;
  passesError?: string | null;
};

const DAY_MIN = 1440;
const MIN_LOOK = 3; // minutes: the narrowest mark drawn, so a two-minute pass is still a visible stroke

export function BlindClock({ model, live, meta }: { model: ClockModel; live: LivePass[]; meta: ClockMeta }) {
  const clientNow = useNow(1_000);
  const now = clientNow ? clientNow.getTime() : meta.at;
  const nowMin = (now - model.dayStart) / 60_000;
  const handDeg = (((nowMin % DAY_MIN) + DAY_MIN) % DAY_MIN) / 4; // 1440 min -> 360 deg

  const { c, ringR, ringW, barIn, barOut, handIn, handOut } = DIAL;
  const r0 = ringR - ringW / 2;
  const r1 = ringR + ringW / 2;
  const gap = model.gap;
  const span_ = gap ? clockSpan(gap.minutes) : null;
  const region = model.scope === 'region';
  const days = meta.days != null ? `the last ${number(meta.days)} days` : 'the last days';

  // who is over the reference site now, who is next (today's computed passes)
  const watching = live.find((p) => p.start <= now && now <= p.end) ?? null;
  const next = live.find((p) => p.start > now) ?? null;
  const ticking = clientNow != null;
  let liveLine: string;
  if (meta.passesError) {
    liveLine = `Today’s passes over ${meta.site} did not load, so the next one cannot be given. ${meta.passesError}`;
  } else if (watching) {
    liveLine = `${watching.satellite} is over ${meta.site} now${ticking ? `, for ${countdown(watching.end - now)} more` : ''}.`;
  } else if (next) {
    liveLine = `No fire satellite over ${meta.site} now. Next: ${next.satellite} at ${timeIST(next.start)} IST${ticking ? `, in ${countdown(next.start - now)}` : ''}.`;
  } else {
    liveLine = `No fire satellite pass over ${meta.site} falls in the next day of the pass list.`;
  }
  const inBite = gap && region && inGap(gap, nowMin) ? ' The hand is in the blind span.' : '';

  const looksDone = model.looks.filter((l) => l.to <= nowMin).length;
  const gapWords = gap && span_ ? `${clockTime(gap.from)} to ${clockTime(gap.to)} IST, ${span_.h} h ${span_.m} min` : null;
  const summary = region
    ? [
        `In ${days} the polar fire satellites reported fires in Punjab and Haryana in ${number(model.looks.length)} windows of the day, ${number(Math.round(model.lookMinutes))} minutes in all.`,
        gapWords ? `None reported a fire from ${gapWords}${gap?.share != null ? `, the span that held ${percent(gap.share)} of GK-2A’s fire activity` : ''}.` : '',
        model.bars.length && meta.peak ? `GK-2A fire activity peaks at ${span(meta.peak)} IST.` : '',
      ]
    : [
        `${number(model.looks.length)} passes of the fire satellites over ${meta.site} on ${dateIST(model.dayStart)} (IST), ${number(Math.round(model.lookMinutes))} minutes in all.`,
        gapWords ? `The longest stretch with no pass runs from ${gapWords}.` : '',
      ];

  return (
    <figure className={s.clock}>
      <svg className={s.clockDial} viewBox="-18 -18 516 516" role="img" aria-labelledby="clock-t clock-d">
        <title id="clock-t">{region ? 'When the polar fire satellites report fires in Punjab and Haryana: a 24-hour clock' : `When the fire satellites pass over ${meta.site} on ${dateIST(model.dayStart)}: a 24-hour clock`}</title>
        <desc id="clock-d">{summary.filter(Boolean).join(' ')}</desc>
        <circle cx={c} cy={c} r={r1 + 16} className="lp-clock-face" />
        {/* the day's ring, bitten where no polar satellite reports a fire */}
        {gap ? (
          <path d={sector(gap.to, gap.to + (DAY_MIN - gap.minutes), r0, r1)} className="lp-clock-ring" />
        ) : (
          <circle cx={c} cy={c} r={ringR} className="lp-clock-ring-full" strokeWidth={ringW} />
        )}
        {/* GK-2A activity by hour */}
        {model.bars.map((b) =>
          b.value > 0 && model.barMax > 0 ? (
            <path
              key={b.hour}
              d={sector(b.hour * 60 + 9, b.hour * 60 + 51, barIn, barIn + Math.max(2, ((barOut - barIn) * b.value) / model.barMax))}
              className="lp-clock-bar"
            />
          ) : null,
        )}
        {/* the fire satellites' looks */}
        {model.looks.map((l) => (
          <path
            key={l.key}
            d={sector(l.from, Math.max(l.to, l.from + MIN_LOOK), r0 - 4, r1 + 4)}
            className="lp-clock-look"
            data-done={!region && l.to > nowMin ? 'no' : 'yes'}
          />
        ))}
        {/* the bite: a thread through the empty stretch of ring */}
        {gap ? <path d={arc(gap.from, gap.from + gap.minutes, ringR)} className="lp-clock-gap" /> : null}
        {/* the hours */}
        {[0, 6, 12, 18].map((h) => {
          const [x, y] = polar(r1 + 6 + (h % 12 === 0 ? 16 : 24), angleOf(h * 60));
          return (
            <text key={h} x={x} y={y} className="lp-clock-hour" textAnchor="middle" dominantBaseline="central" aria-hidden="true">
              {String(h).padStart(2, '0') /* a clock's hours; taste-skill-ignore */}
            </text>
          );
        })}
        {/* the hub: the blind span. Its lines sit above and below the centre so the hand never crosses them. */}
        {gap && span_ ? (
          <g aria-hidden="true">
            <text x={c} y={c - 26} className="lp-clock-hub-word" textAnchor="middle">
              No fire alerts
            </text>
            <text x={c} y={c + 48} className="lp-clock-hub-big" textAnchor="middle">
              {`${span_.h} h ${String(span_.m).padStart(2, '0')} min` /* taste-skill-ignore */}
            </text>
            <text x={c} y={c + 78} className="lp-clock-hub-time" textAnchor="middle">
              {`${clockTime(gap.from)} to ${clockTime(gap.to)}`}
            </text>
          </g>
        ) : null}
        {/* now */}
        <g transform={`translate(${c} ${c})`} aria-hidden="true">
          <g className="lp-clock-hand" style={{ ['--a' as string]: `${handDeg.toFixed(2)}deg` }}>
            <line x1={0} y1={-handIn} x2={0} y2={-handOut} />
            <circle cx={0} cy={-handOut} r={5} />
          </g>
        </g>
      </svg>
      <figcaption className={s.clockKey}>
        <p className={s.clockLive} aria-live="off">
          <span className={`${s.swatch} lp-swatch-now`} aria-hidden="true" />
          {`Now ${timeIST(now)} IST.${inBite} ${liveLine}`}
        </p>
        <p>
          <span className={`${s.swatch} lp-swatch-look`} aria-hidden="true" />
          {region
            ? `Gold: when a polar fire satellite (VIIRS, MODIS) reported a fire in Punjab or Haryana in ${days}. The bite: ${gapWords ? `${gapWords}, ` : ''}no fire alert from any of them.`
            : `Gold: today’s passes of the fire satellites over ${meta.site}, ${number(model.looks.length)} passes and ${number(Math.round(model.lookMinutes))} minutes in all${model.looks.length ? `; ${number(looksDone)} done` : ''}. The polar windows over the region did not load${meta.diurnalError ? ` (${meta.diurnalError.replace(/\.$/, '')})` : ''}.`}
        </p>
        <p>
          <span className={`${s.swatch} lp-swatch-fire`} aria-hidden="true" />
          {model.bars.length
            ? `Red: fire activity by hour from GK-2A, a geostationary satellite that scans every ten minutes but is too coarse to count fires (an activity index)${meta.days ? `, mean of ${days}` : ''}.${region && meta.share != null ? ` ${percent(meta.share)} of it fell outside the gold${gap?.share != null ? `, ${percent(gap.share)} inside the bite` : ''}.` : ''}`
            : `Red: GK-2A’s fire activity by hour did not load${meta.diurnalError ? `. ${meta.diurnalError}` : '.'}`}
        </p>
      </figcaption>
    </figure>
  );
}
