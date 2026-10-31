'use client';

import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { s } from './styles';

export type Frame = { src: string; width: number; height: number; date: string; dateLabel: string; satellite: string | null; alt: string };

const HOLD_MS = 5_500;

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
 * oldest first, each held about five and a half seconds and crossfaded over 1.6 s, so the black scars
 * appear one pass at a time. Paused while the tab is hidden. Under reduced motion it holds the newest
 * pass, where the scars are. The dates below are buttons: choosing one stops the cycle there.
 *
 * Every frame is in the server HTML, so without script the oldest pass shows and the dates still read.
 * Only the pass on screen downloads at first; the others start once it has arrived, so on a slow
 * phone link they do not race the first picture and the fonts for the bandwidth.
 */
export function Crossfade({ frames, place }: { frames: Frame[]; place: string }) {
  const [i, setI] = useState(0);
  const [held, setHeld] = useState(false);
  const reduce = useSyncExternalStore(subscribeReduce, getReduce, () => false);
  // Under reduced motion, until a date is chosen, hold the newest pass.
  const shown = reduce && !held ? frames.length - 1 : i;
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
    if (img && img.complete && img.naturalWidth === 0 && img.getAttribute('src')) fail(0);
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
  }, [warm, reduce]);

  useEffect(() => {
    if (held || reduce || !warm || frames.length < 2) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (!timer) timer = setInterval(() => setI((n) => (n + 1) % frames.length), HOLD_MS + FADE_MS);
    };
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const onVis = () => (document.visibilityState === 'hidden' ? stop() : start());
    start();
    document.addEventListener('visibilitychange', onVis);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [held, reduce, warm, frames.length]);

  return (
    <>
      <div className={s.frames}>
        {frames.map((f, n) => (
          // eslint-disable-next-line @next/next/no-img-element -- API renders shown at their own pixel size; next/image would add nothing
          <img
            key={f.src}
            ref={n === 0 ? first : undefined}
            src={n === 0 || n === shown || warm ? f.src : undefined}
            width={f.width}
            height={f.height}
            alt={n === shown ? f.alt : ''}
            aria-hidden={n === shown ? undefined : true}
            className={s.frame}
            data-on={n === shown ? 'yes' : 'no'}
            hidden={failed.has(n)}
            onError={() => fail(n)}
            fetchPriority={n === 0 ? 'high' : 'auto'}
            decoding="async"
          />
        ))}
        {failed.has(shown) ? (
          <div className={s.picMissing} style={{ position: 'absolute', inset: 0, height: 'auto' }}>
            <p>This Sentinel-2 picture did not load from the API.</p>
          </div>
        ) : null}
      </div>
      <div className={s.frameCaption}>
        <p className={s.frameWhere}>{place}</p>
        <ul className={s.frameDates} aria-label="Sentinel-2 passes">
          {frames.map((f, n) => (
            <li key={f.date}>
              <button
                type="button"
                className={s.frameDate}
                aria-pressed={n === shown}
                onClick={() => {
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
      </div>
    </>
  );
}
