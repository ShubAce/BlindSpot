'use client';

import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { dateIST, metricText, number } from '@/lib/format';

import { s } from './styles';

/** Per frame: the outlines that first show on it (gold = no fire alert, white = FIRMS-confirmed, as
 *  SVG paths in the picture's pixels) and the running counts the caption reads. */
export type FrameMarks = {
  groups: { unseen: string; seen: string }[];
  tally: { unseen: number; seen: number; unchecked: number; ha: number }[];
  since: string | null;
  truncated: boolean;
};

export type Frame = { src: string; width: number; height: number; date: string; dateLabel: string; satellite: string | null; alt: string };

/** An earlier pass is held this long once faded in; the newest (where every outline is) four times as
 *  long, so most glances land on the evidence. */
const HOLD_MS = 3_800;
const NEWEST_HOLD_MS = 15_000;

const REDUCE = '(prefers-reduced-motion: reduce)';
const subscribeReduce = (cb: () => void) => {
  const m = window.matchMedia(REDUCE);
  m.addEventListener('change', cb);
  return () => m.removeEventListener('change', cb);
};
const getReduce = () => window.matchMedia(REDUCE).matches;
const FADE_MS = 1_600;

/**
 * The signature move (direction sheet, "Slow crossfade"): the same fields on four Sentinel-2 passes,
 * crossfaded over 1.6 s. It opens on the newest pass, where every outline is (the evidence first, for a
 * juror who glances), and holds it 15 s; then it runs from the oldest pass up, about 4 s each, so the
 * black scars and their outlines appear one pass at a time, and comes back to the newest. Only the
 * incoming pass fades: the outgoing one stays fully opaque under it until the next change, so the page
 * never shows through mid-fade (on the light theme it would wash the picture out). Paused while the tab
 * is hidden. Under reduced motion it holds the newest pass. The dates below are buttons: choosing one
 * stops the cycle there.
 *
 * Every frame is in the server HTML, so without script the newest pass shows and the dates still read.
 * Only the pass on screen downloads at first; the others start once it has arrived, so on a slow
 * phone link they do not race the first picture and the fonts for the bandwidth.
 */
export function Crossfade({ frames, place, marks, marksNote }: { frames: Frame[]; place: string; marks: FrameMarks | null; marksNote: string | null }) {
  const last = frames.length - 1;
  const [i, setI] = useState(last);
  // the pass faded out of: kept opaque under the incoming one (see the comment above)
  const [prev, setPrev] = useState<number | null>(null);
  const [held, setHeld] = useState(false);
  const reduce = useSyncExternalStore(subscribeReduce, getReduce, () => false);
  // Under reduced motion, until a date is chosen, hold the newest pass.
  const shown = reduce && !held ? last : i;
  const [warm, setWarm] = useState(false);
  const first = useRef<HTMLImageElement>(null);
  // passes whose picture failed: hidden, and the frame says so in one line instead of a broken image
  const [failed, setFailed] = useState<ReadonlySet<number>>(() => new Set());
  const fail = (n: number) => setFailed((f) => (f.has(n) ? f : new Set(f).add(n)));

  useEffect(() => {
    if (warm) return;
    const img = first.current;
    const go = () => setWarm(true);
    // the first frame can fail before hydration, when React has no handler for it yet
    if (img && img.complete && img.naturalWidth === 0 && img.getAttribute('src')) fail(last);
    if (!img || img.complete || reduce) {
      go();
      return;
    }
    img.addEventListener('load', go, { once: true });
    img.addEventListener('error', go, { once: true });
    // never wait longer than a cycle for a picture that is slow to arrive
    const t = setTimeout(go, HOLD_MS);
    return () => {
      img.removeEventListener('load', go);
      img.removeEventListener('error', go);
      clearTimeout(t);
    };
  }, [warm, reduce, last]);

  useEffect(() => {
    if (held || reduce || !warm || frames.length < 2) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const wait = FADE_MS + (i === last ? NEWEST_HOLD_MS : HOLD_MS);
    const start = () => {
      if (timer) return;
      timer = setTimeout(() => {
        setPrev(i);
        setI((i + 1) % frames.length);
      }, wait);
    };
    const stop = () => {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    const onVis = () => (document.visibilityState === 'hidden' ? stop() : start());
    if (document.visibilityState !== 'hidden') start();
    document.addEventListener('visibilitychange', onVis);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [held, reduce, warm, frames.length, i, last]);

  const t = marks?.tally[shown];
  const w = frames[0].width;
  const h = frames[0].height;
  const count = t ? tallyLine(frames[shown].dateLabel, t, marks?.truncated ?? false) : marksNote;

  return (
    <>
      <div className={s.shot} style={{ aspectRatio: `${w} / ${h}` }}>
        <div className={s.frames}>
          {frames.map((f, n) => (
            // eslint-disable-next-line @next/next/no-img-element -- API renders shown at their own pixel size; next/image would add nothing
            <img
              key={f.src}
              ref={n === last ? first : undefined}
              src={n === last || n === shown || warm ? f.src : undefined}
              width={f.width}
              height={f.height}
              alt={n === shown ? f.alt : ''}
              aria-hidden={n === shown ? undefined : true}
              className={s.frame}
              data-on={n === shown ? 'yes' : n === prev ? 'under' : 'no'}
              hidden={failed.has(n)}
              onError={() => fail(n)}
              fetchPriority={n === last ? 'high' : 'auto'}
              decoding="async"
            />
          ))}
          {marks ? (
            <svg className={s.marks} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid slice" aria-hidden="true">
              {marks.groups.map((g, n) => (
                <g key={n} data-on={n <= shown ? 'yes' : 'no'}>
                  {g.seen ? <path d={g.seen} className="lp-mark-seen" /> : null}
                  {g.unseen ? <path d={g.unseen} className="lp-mark-unseen" /> : null}
                </g>
              ))}
            </svg>
          ) : null}
          {failed.has(shown) ? (
            <div className={s.picMissing} style={{ position: 'absolute', inset: 0, zIndex: 4, height: 'auto' }}>
              <p>This Sentinel-2 picture did not load from the API.</p>
            </div>
          ) : null}
        </div>
      </div>
      <div className={s.frameCaption}>
        <ul className={s.frameDates} aria-label="Sentinel-2 passes">
          {frames.map((f, n) => (
            <li key={f.date}>
              <button
                type="button"
                className={s.frameDate}
                aria-pressed={n === shown}
                onClick={() => {
                  if (n !== shown) setPrev(shown);
                  setHeld(true);
                  setI(n);
                }}
              >
                {f.dateLabel}
                {f.satellite ? <span className="sr-only">, {f.satellite}</span> : null}
              </button>
            </li>
          ))}
        </ul>
        {count ? <p className={s.frameCount}>{count}</p> : null}
        <p className={s.frameWhere}>
          {place}
          {marks?.since ? ` Outlines: burn scars mapped since ${dateIST(marks.since)}.` : ''}
        </p>
      </div>
    </>
  );
}

/** The caption's count for one frame, in words a juror reads at a glance: "By 28 Oct: 115 burn scars
 *  in this frame (913 ha). 21 had a fire alert (white); 94 had none, candidate unseen burns (gold)." */
export function tallyLine(date: string, t: FrameMarks['tally'][number], truncated: boolean): string {
  const all = t.seen + t.unseen + t.unchecked;
  const scars = (n: number) => `${number(n)} burn scar${n === 1 ? '' : 's'}`;
  if (!all) return `By ${date}: no burn scar mapped in this frame yet.`;
  // "touching": the box query returns every scar that reaches into the frame, whole, so a few lie
  // partly outside it (the /method caption counts only those centred inside, hence its smaller total)
  const head = `By ${date}: ${truncated ? 'at least ' : ''}${scars(all)} touching this frame (${metricText('area_ha', t.ha)}).`;
  const unseen = `candidate unseen burn${t.unseen === 1 ? '' : 's'} (gold)`;
  let body: string;
  if (!t.seen && t.unseen) {
    if (t.unseen < all) body = `${number(t.unseen)} had no fire alert: ${unseen}.`;
    else body = all === 1 ? 'It had no fire alert: a candidate unseen burn (gold).' : `None had a fire alert: ${all === 2 ? 'both' : `all ${number(all)}`} are ${unseen}.`;
  }
  else if (t.seen && !t.unseen) body = `${t.seen === all ? (all === 1 ? 'It' : 'All') : number(t.seen)} had a fire alert (white).`;
  else if (t.seen && t.unseen) body = `${number(t.seen)} had a fire alert (white); ${number(t.unseen)} had none, ${unseen}.`;
  else body = '';
  const rest = t.unchecked ? ` ${number(t.unchecked)} not yet checked against the alerts.` : '';
  return `${head}${body ? ` ${body}` : ''}${rest}`;
}
