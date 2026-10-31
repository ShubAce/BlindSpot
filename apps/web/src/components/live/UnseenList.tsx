'use client';

import { useMemo, useState } from 'react';

import { EmptyNote, ErrorNote, Freshness, Skeleton } from '@/components/ui/states';
import { dateIST, metricText, number } from '@/lib/format';

import { precisionText, useLiveHeadline, useLiveScars, useSourceCadence } from './data';
import { useLiveActions, useLiveState } from './live-state';
import styles from './styles';
import { useMapCommands } from './map-commands';
import { scarState } from './scar-state';

const FIRST = 10;
const MORE = 20;

/** "28–30 Oct" or "30 Sep – 2 Oct": the window between the clear pass before and the one after. */
function burnWindow(after: string, before: string): string {
  const a = dateIST(after);
  const b = dateIST(before);
  const [ad, am] = a.split(/\s| /);
  const [bd, bm] = b.split(/\s| /);
  return am === bm ? `${ad}–${bd} ${bm}` : `${a} – ${b}`;
}

/**
 * The ranked list of candidate unseen burns among the scars on the map: largest and most confident
 * first (area times confidence). Hover outlines a field on the map; a click flies to it and opens
 * its case summary. On a phone it is a sheet over the bottom of the screen.
 */
export function UnseenList() {
  const s = useLiveState();
  const { dispatch } = useLiveActions();
  const commands = useMapCommands();
  const scars = useLiveScars(s.replayDay);
  const head = useLiveHeadline();
  const cadence = useSourceCadence();
  const [shown, setShown] = useState(FIRST);
  const [open, setOpen] = useState(false);

  const ranked = useMemo(() => {
    const rows = (scars.data?.features.features ?? []).filter((f) => scarState(f.properties) === 'unseen');
    return rows
      .map((f) => ({ f, score: (f.properties.area_ha ?? 0) * Number(f.properties.confidence ?? 0) }))
      .sort((a, b) => b.score - a.score)
      .map((r) => r.f);
  }, [scars.data]);

  const window = useMemo(() => {
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

  const sub = window ? `burnt ${burnWindow(window.a, window.b)}` : null;

  return (
    <section className={styles.listCard} data-open={open || undefined} aria-labelledby="unseen-title">
      <button type="button" className={styles.sheetHandle} aria-expanded={open} aria-controls="unseen-body" onClick={() => setOpen((v) => !v)}>
        <span>Candidate unseen burns{scars.data ? ` · ${number(ranked.length)}${scars.data.truncated ? ' newest' : ''}` : ''}</span>
        <span className={styles.faint}>{open ? 'Close' : 'Open'}</span>
      </button>
      <div id="unseen-body" className={styles.listInner}>
        <header className={styles.cardHead}>
          <h2 id="unseen-title" className={styles.cardTitle}>
            Candidate unseen burns
          </h2>
          {scars.data ? <Freshness resp={scars.data} cadenceMs={cadence('stac_e84')} /> : null}
        </header>
        <div className={styles.listMeta}>
          {scars.error && !scars.data ? (
            <ErrorNote what="burn scars" error={scars.error} onRetry={() => void scars.mutate()} />
          ) : !scars.data ? (
            <>
              <Skeleton width="90%" height={14} />
              <Skeleton width="70%" height={14} />
            </>
          ) : (
            <>
              {scars.data.truncated ? (
                // the map holds the newest scars only: say so, so a page of rows is never read as the whole count
                <p>
                  The newest <span className="nums">{number(scars.data.features.features.length)}</span> of <span className="nums">{number(scars.data.totals.clusters)}</span> mapped scars hold{' '}
                  <strong className="nums">{number(ranked.length)}</strong> with no fire alert, <strong className="nums">{metricText('area_ha', area)}</strong>
                  {sub ? `, ${sub}` : ''}; largest and most confident first.
                </p>
              ) : (
                <p>
                  <strong className="nums">{metricText('area_ha', area)}</strong> in <span className="nums">{number(ranked.length)}</span> fields with no fire alert{sub ? `, ${sub}` : ''}; largest and most confident first.
                </p>
              )}
              <p className={styles.faint}>
                {precision ? `${precision.charAt(0).toUpperCase()}${precision.slice(1)}.` : head.data ? 'The label has no audited precision yet.' : <Skeleton width={220} height={12} />}
              </p>
            </>
          )}
        </div>

        {scars.data && ranked.length === 0 ? (
          <EmptyNote style={{ padding: '0 var(--card-pad)' }}>
            {s.replayDay ? `No candidate unseen burns among the scars up to ${dateIST(`${s.replayDay}T12:00:00+05:30`)}.` : 'No candidate unseen burns among the newest scars.'}
          </EmptyNote>
        ) : null}

        <ol className={styles.rows}>
          {!scars.data && !scars.error
            ? Array.from({ length: 6 }, (_, i) => (
                <li key={i} className={styles.rowSkeleton}>
                  <Skeleton width="55%" height={14} />
                  <Skeleton width="80%" height={12} />
                </li>
              ))
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
                          {p.district ?? 'District unknown'} · {burnWindow(p.burn_after, p.burn_before)} · confidence {number(Number(p.confidence ?? 0), { decimals: 2 })}
                        </span>
                      </span>
                      <span className={`${styles.rowValue} nums`}>{metricText('area_ha', p.area_ha)}</span>
                    </button>
                  </li>
                );
              })}
        </ol>
        {ranked.length > shown ? (
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
