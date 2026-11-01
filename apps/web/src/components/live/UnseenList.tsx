'use client';

import { useMemo, useState } from 'react';

import { EmptyNote, ErrorNote, Freshness, Skeleton } from '@/components/ui/states';
import { DAILY_RUN_STALE_MS } from '@/lib/cadence';
import { dateIST, dateRangeIST, istDayKey, metricText, number } from '@/lib/format';

import { precisionText, SCAR_QUERY, useDayRead, useLiveFires, useLiveHeadline, useLiveScars } from './data';
import { useLiveActions, useLiveState } from './live-state';
import styles from './styles';
import { useMapCommands } from './map-commands';
import { scarState } from './scar-state';
import { useScrollCue } from './scroll-cue';

const FIRST = 10;
const MORE = 20;


/**
 * What a replay day held when it held no candidate unseen burn: the day's fire alerts and the scars
 * mapped by then, and, before the season's first Sentinel-2 pass, that there was nothing to map yet.
 */
function ReplayDayCounts({ day, scars, fires, firstPass, replayDay }: { day: string; scars: number; fires: number | null; firstPass: string | null; replayDay: string | null }) {
  // the pass's IST day against the replay day, both YYYY-MM-DD
  const passDay = firstPass ? istDayKey(firstPass) : null;
  const beforeFirst = passDay != null && replayDay != null && replayDay < passDay;
  return (
    <>
      <dl className={styles.dayCounts}>
        <div>
          <dt>Fire alerts on {day}</dt>
          <dd className="nums">{fires == null ? <Skeleton width={40} height={14} /> : number(fires)}</dd>
        </div>
        <div>
          <dt>Scars mapped up to {day}</dt>
          <dd className="nums">{number(scars)}</dd>
        </div>
      </dl>
      {beforeFirst && firstPass ? <p className={styles.faint}>The season’s Sentinel-2 scars start with the pass of {dateIST(firstPass)}: before it there was no picture to find one in.</p> : null}
    </>
  );
}

/**
 * The ranked list of candidate unseen burns among the scars on the map: largest and most confident
 * first (area times confidence). Hover outlines a field on the map; a click flies to it and opens
 * its case summary. On a phone it is a sheet over the bottom of the screen.
 *
 * While a replay day loads, the last day's rows stay in place, dimmed and out of reach, under a line
 * that says which day is loading: the list neither blinks empty at each step of Play nor shows one
 * day's figures under another day's name.
 */
export function UnseenList() {
  const s = useLiveState();
  const { dispatch } = useLiveActions();
  const commands = useMapCommands();
  const scars = useLiveScars(s.replayDay);
  // the day's own answer: what every figure below is read from
  const { held, own } = useDayRead('scars', s.replayDay, scars);
  const head = useLiveHeadline();
  const [shown, setShown] = useState(FIRST);
  const [open, setOpen] = useState(false);
  const dayLabel = s.replayDay ? dateIST(`${s.replayDay}T12:00:00+05:30`) : null;
  // the rows scroll inside the card: the edge with more of them fades (scroll-cue.ts)
  const rowsCue = useScrollCue<HTMLOListElement>('y');

  const ranked = useMemo(() => {
    const rows = (scars.data?.features.features ?? []).filter((f) => scarState(f.properties) === 'unseen');
    return rows
      .map((f) => ({ f, score: (f.properties.area_ha ?? 0) * Number(f.properties.confidence ?? 0) }))
      .sort((a, b) => b.score - a.score)
      .map((r) => r.f);
  }, [scars.data]);

  const span = useMemo(() => {
    let a: string | null = null;
    let b: string | null = null;
    for (const f of ranked) {
      if (!a || f.properties.burn_after < a) a = f.properties.burn_after;
      if (!b || f.properties.burn_before > b) b = f.properties.burn_before;
    }
    return a && b ? { a, b } : null;
  }, [ranked]);
  const area = useMemo(() => ranked.reduce((t, f) => t + (f.properties.area_ha ?? 0), 0), [ranked]);
  const precision = precisionText(head.data?.unseen_burns);
  // a replay day with no candidates yet still has its own figures: its fire alerts (the same read the
  // map draws) and the scars mapped by then
  const firesOwn = useDayRead('fires', s.replayDay, useLiveFires(s.replayDay)).own;
  const dayFires = s.replayDay && firesOwn ? firesOwn.count : null;

  const sub = span ? `burnt ${dateRangeIST(span.a, span.b)}` : null;

  return (
    <section className={styles.listCard} data-open={open || undefined} aria-labelledby="unseen-title">
      <button type="button" className={styles.sheetHandle} aria-expanded={open} aria-controls="unseen-body" onClick={() => setOpen((v) => !v)}>
        <span>Candidate unseen burns{own ? ` · ${number(ranked.length)}${own.truncated ? ' newest' : ''}` : ''}</span>
        <span className={styles.faint}>{open ? 'Close' : 'Open'}</span>
      </button>
      <div id="unseen-body" className={styles.listInner}>
        <header className={styles.cardHead}>
          <h2 id="unseen-title" className={styles.cardTitle}>
            Candidate unseen burns
          </h2>
          {/* the map key's rule for the same read: scars change with the daily Sentinel-2 run */}
          {own ? <Freshness resp={own} staleAfterMs={DAILY_RUN_STALE_MS} /> : null}
        </header>
        <div className={styles.listMeta}>
          {scars.error && !own ? (
            <ErrorNote what={dayLabel ? `the burn scars up to ${dayLabel}` : 'burn scars'} error={scars.error} onRetry={() => void scars.mutate()} />
          ) : held ? (
            <p className={styles.faint}>Loading the scars mapped up to {dayLabel ?? 'today'}…</p>
          ) : !own ? (
            <>
              <Skeleton width="90%" height={14} />
              <Skeleton width="70%" height={14} />
            </>
          ) : ranked.length === 0 ? (
            <>
              <EmptyNote>{dayLabel ? `No candidate unseen burns among the scars mapped up to ${dayLabel}.` : 'No candidate unseen burns among the newest scars.'}</EmptyNote>
              {dayLabel ? <ReplayDayCounts day={dayLabel} scars={own.totals.clusters} fires={dayFires} firstPass={head.data?.unseen_burns?.first_pass ?? null} replayDay={s.replayDay} /> : null}
            </>
          ) : (
            <>
              {own.truncated ? (
                // the map holds the newest scars only: say so, so a page of rows is never read as the whole count
                <p>
                  The newest <span className="nums">{number(own.features.features.length)}</span> of <span className="nums">{number(own.totals.clusters)}</span> scars mapped at confidence{' '}
                  {number(SCAR_QUERY.min_confidence, { decimals: 1 })} or more{dayLabel ? ` up to ${dayLabel}` : ''} hold{' '}
                  <strong className="nums">{number(ranked.length)}</strong> with no fire alert, <strong className="nums">{metricText('area_ha', area)}</strong>
                  {sub ? `, ${sub}` : ''}; largest and most confident first.
                </p>
              ) : (
                <p>
                  <strong className="nums">{metricText('area_ha', area)}</strong> in <span className="nums">{number(ranked.length)}</span> fields with no fire alert{sub ? `, ${sub}` : ''}
                  {dayLabel ? `, mapped up to ${dayLabel}` : ''}; largest and most confident first.
                </p>
              )}
            </>
          )}
          {/* the audit qualifies every candidate listed, whichever day the list holds */}
          {(own && ranked.length > 0) || held ? (
            <p className={styles.faint}>
              {precision ? `${precision.charAt(0).toUpperCase()}${precision.slice(1)}.` : head.data ? 'The label has no audited precision yet.' : <Skeleton width={220} height={12} />}
            </p>
          ) : null}
        </div>

        <ol className={styles.rows} ref={rowsCue} data-held={held || undefined} inert={held || undefined}>
          {!scars.data && !scars.error
            ? Array.from({ length: 6 }, (_, i) => (
                <li key={i} className={styles.rowSkeleton}>
                  <Skeleton width="55%" height={14} />
                  <Skeleton width="80%" height={12} />
                </li>
              ))
            : scars.error && !own
              ? null // the day's read failed: the note above says so, with no other day's rows under it
              : ranked.slice(0, shown).map((f, i) => {
                  const p = f.properties;
                  const selected = s.selectedScar === p.id;
                  return (
                    <li key={p.id}>
                      <button
                        type="button"
                        className={styles.row}
                        aria-current={selected || undefined}
                        onMouseEnter={() => dispatch({ type: 'hover', id: p.id })}
                        onMouseLeave={() => dispatch({ type: 'hover', id: null })}
                        onFocus={() => dispatch({ type: 'hover', id: p.id })}
                        onBlur={() => dispatch({ type: 'hover', id: null })}
                        onClick={() => {
                          commands.openCase(p.id);
                          setOpen(false);
                        }}
                      >
                        <span className={styles.rank}>{i + 1}</span>
                        <span className={styles.rowMain}>
                          <span className={styles.rowName}>{p.village ? `Near ${p.village}` : p.subdistrict ?? 'Unnamed place'}</span>
                          <span className={styles.rowSub}>
                            {p.district ?? 'District unknown'} · {dateRangeIST(p.burn_after, p.burn_before)} · confidence {number(Number(p.confidence ?? 0), { decimals: 2 })}
                          </span>
                        </span>
                        <span className={`${styles.rowValue} nums`}>{metricText('area_ha', p.area_ha)}</span>
                      </button>
                    </li>
                  );
                })}
        </ol>
        {own && ranked.length > shown ? (
          <div className={styles.listFoot}>
            <button type="button" className="text-button" onClick={() => setShown((n) => n + MORE)}>
              Show {number(Math.min(MORE, ranked.length - shown))} more of {number(ranked.length)}{scars.data?.truncated ? ' newest' : ''}
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
