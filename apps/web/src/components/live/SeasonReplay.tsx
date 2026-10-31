'use client';

import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from 'react';

import { ErrorNote, Skeleton } from '@/components/ui/states';
import { ticks } from '@/lib/charts';
import { dateIST, number } from '@/lib/format';

import { useFiresDailyLive } from './data';
import { useLiveActions, useLiveState } from './live-state';
import styles from './styles';

/**
 * The season replay: a slider from the season's first day to today that scrubs the map. Its track
 * is the chart of FIRMS fire detections per IST day (/api/fires/daily), partial days drawn as
 * outlines; the day chosen is marked by the one gold line. The last position is live. It is a
 * native range input, so arrows, Page Up/Down, Home and End all work; Play steps a day at a time.
 */

const BARS_H = 34;
const AXIS_H = 18;
const STEP_MS = 1_400;
/** Wait this long after the slider stops before asking for that day's scars and fires. */
const SETTLE_MS = 220;

const fill = (token: string): CSSProperties => ({ fill: `var(${token})` });

export function SeasonReplay() {
  const s = useLiveState();
  const { dispatch } = useLiveActions();
  const daily = useFiresDailyLive();
  const series = useMemo(() => daily.data?.series ?? [], [daily.data]);
  const n = series.length;
  // the last day in the series is today (partial): that position is live
  const liveIndex = n - 1;

  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.round(el.clientWidth)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // the slider moves at once; the map follows when it settles
  const committed = s.replayDay ? series.findIndex((d) => d.date === s.replayDay) : liveIndex;
  const [pos, setPos] = useState<number | null>(null);
  const shown = pos ?? committed;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const commit = useCallback(
    (i: number, delay = SETTLE_MS) => {
      setPos(i);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        dispatch({ type: 'replay', day: i >= liveIndex || i < 0 ? null : series[i].date });
        setPos(null);
      }, delay);
    },
    [dispatch, liveIndex, series],
  );
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      const cur = s.replayDay ? series.findIndex((d) => d.date === s.replayDay) : liveIndex;
      const next = cur >= liveIndex ? 0 : cur + 1;
      if (next >= liveIndex) setPlaying(false);
      commit(next, 0);
    }, STEP_MS);
    return () => clearInterval(t);
  }, [playing, s.replayDay, series, liveIndex, commit]);

  const [hover, setHover] = useState<number | null>(null);
  const id = useId();

  if (daily.error && !daily.data) {
    return (
      <div className={styles.replay}>
        <h3 className={styles.replayTitle}>Season replay</h3>
        <ErrorNote what="the fires per day" error={daily.error} onRetry={() => void daily.mutate()} />
      </div>
    );
  }

  const max = Math.max(1, ...series.map((d) => d.count));
  const yTicks = ticks(0, max, 4);
  const top = yTicks[yTicks.length - 1];
  const W = width;
  const bw = n ? W / n : 0;
  const x = (i: number) => i * bw;
  const h = (v: number) => (v / top) * BARS_H;
  const focus = hover ?? shown;
  const fd = focus >= 0 && focus < n ? series[focus] : null;
  const live = shown >= liveIndex;
  const monthTicks = series
    .map((d, i) => ({ d, i }))
    .filter(({ d }) => d.date.endsWith('-01') || d.date.endsWith('-15'));

  const readout = !fd
    ? null
    : `${dateIST(`${fd.date}T12:00:00+05:30`)}${focus === liveIndex ? ' (today so far)' : ''}: ${number(fd.count)} fire detection${fd.count === 1 ? '' : 's'}${fd.complete ? '' : ', partial day'}`;

  return (
    <div className={styles.replay}>
      <div className={styles.replayHead}>
        <h3 className={styles.replayTitle} id={`${id}-t`}>
          Season replay
        </h3>
        <p className={styles.replayReadout} aria-live="polite">
          {n ? (live && hover == null ? 'Live: fire alerts of the last 24 h and the newest scars' : readout) : <Skeleton width={260} height={14} />}
        </p>
        <ul className={styles.replayKey} aria-label="Key">
          <li>
            <svg width="10" height="10" aria-hidden="true">
              <rect x="0" y="0" width="10" height="10" style={{ fill: 'var(--mark-fire)' }} />
            </svg>
            Complete day
          </li>
          <li>
            <svg width="10" height="10" aria-hidden="true">
              <rect x="0.5" y="0.5" width="9" height="9" strokeWidth={1} style={{ fill: 'none', stroke: 'var(--mark-fire)' }} />
            </svg>
            Partial day
          </li>
        </ul>
        <div className={styles.replayControls}>
          <button
            type="button"
            className={styles.ctrl}
            onClick={() => setPlaying((p) => !p)}
            disabled={!n}
            aria-label={playing ? 'Pause the replay' : 'Play the season from the first day'}
          >
            {playing ? (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M3 2v8M9 2v8" stroke="currentColor" strokeWidth="2" />
              </svg>
            ) : (
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M3 1.5l7 4.5-7 4.5z" fill="currentColor" />
              </svg>
            )}
            <span>{playing ? 'Pause' : 'Play'}</span>
          </button>
          <button type="button" className={styles.ctrl} onClick={() => { setPlaying(false); commit(liveIndex, 0); }} disabled={!n} aria-pressed={live}>
            <span className={live ? 'selected-line' : undefined}>Live</span>
          </button>
        </div>
      </div>

      <div className={styles.replayTrack} ref={box} style={{ height: BARS_H + AXIS_H + 4 }}>
        {n && W ? (
          <svg width={W} height={BARS_H + AXIS_H + 4} aria-hidden="true" style={{ display: 'block', overflow: 'visible' }}>
            {/* one faint line at the top tick, labelled, so the columns have a scale */}
            <line x1={0} x2={W} y1={4 + BARS_H - h(top)} y2={4 + BARS_H - h(top)} strokeWidth={1} strokeDasharray="3 3" style={{ stroke: 'var(--grid)' }} />
            <text x={0} y={4 + BARS_H - h(top) - 4} fontSize={11} style={fill('--axis-ink')}>
              {number(top)} FIRMS detections a day
            </text>
            {series.map((d, i) => {
              const bh = Math.max(d.count > 0 ? 1 : 0, h(d.count));
              const gap = bw > 6 ? 2 : 1;
              const past = i <= shown;
              return d.complete ? (
                <rect key={d.date} x={x(i) + gap / 2} y={4 + BARS_H - bh} width={Math.max(1, bw - gap)} height={bh} style={{ fill: 'var(--mark-fire)', opacity: past ? 1 : 0.35 }} />
              ) : (
                <rect
                  key={d.date}
                  x={x(i) + gap / 2 + 0.5}
                  y={4 + BARS_H - bh + 0.5}
                  width={Math.max(1, bw - gap - 1)}
                  height={Math.max(0, bh - 1)}
                  strokeWidth={1}
                  style={{ fill: 'none', stroke: 'var(--mark-fire)', opacity: past ? 1 : 0.35 }}
                />
              );
            })}
            <line x1={0} x2={W} y1={4 + BARS_H + 0.5} y2={4 + BARS_H + 0.5} strokeWidth={1} style={{ stroke: 'var(--chart-track)' }} />
            {monthTicks.map(({ d, i }) => {
              const lab = dateIST(`${d.date}T12:00:00+05:30`);
              const tx = Math.min(W - lab.length * 6.2, Math.max(0, x(i)));
              return (
                <text key={d.date} x={tx} y={4 + BARS_H + 14} fontSize={11} style={fill('--axis-ink')}>
                  {lab}
                </text>
              );
            })}
            {/* the chosen day: the one gold line */}
            {shown >= 0 ? <line x1={x(shown) + bw / 2} x2={x(shown) + bw / 2} y1={0} y2={4 + BARS_H} strokeWidth={1} style={{ stroke: 'var(--accent)' }} /> : null}
            {hover != null && hover !== shown ? <line x1={x(hover) + bw / 2} x2={x(hover) + bw / 2} y1={0} y2={4 + BARS_H} strokeWidth={1} style={{ stroke: 'var(--muted)' }} /> : null}
          </svg>
        ) : (
          <Skeleton width="100%" height={BARS_H} />
        )}
        {n ? (
          <input
            type="range"
            className={styles.replayRange}
            min={0}
            max={liveIndex}
            step={1}
            value={Math.max(0, shown)}
            aria-labelledby={`${id}-t`}
            aria-valuetext={live ? 'Live' : readout ?? undefined}
            onChange={(e) => {
              setPlaying(false);
              commit(Number(e.target.value));
            }}
            onPointerMove={(e) => {
              if (!W || !bw) return;
              const r = e.currentTarget.getBoundingClientRect();
              setHover(Math.max(0, Math.min(n - 1, Math.floor((e.clientX - r.left) / bw))));
            }}
            onPointerLeave={() => setHover(null)}
          />
        ) : null}
      </div>
    </div>
  );
}
