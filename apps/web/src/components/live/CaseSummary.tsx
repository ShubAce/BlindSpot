'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { scarState } from './scar-state';
import { ErrorNote, Skeleton } from '@/components/ui/states';
import { absoluteApiUrl } from '@/lib/api';
import { dateIST, dateRangeIST, metricText, number, percent, placeName } from '@/lib/format';

import { precisionText, useLiveHeadline, useScarCaseLive } from './data';
import { useLiveState } from './live-state';
import styles from './styles';
import { useMapCommands } from './map-commands';
import { useScrollCue } from './scroll-cue';
import type { ImagerySide } from './types';

const CHIP = 132;

function Chip({ side, when }: { side: ImagerySide | null; when: string }) {
  const url = absoluteApiUrl(side?.truecolor_url);
  // A render the API refused (busy, rate-limited) arrives as an error body, not a picture: say so
  // instead of a broken image. Keyed by the URL, so another case's chip starts afresh.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const failed = url != null && failedUrl === url;
  return (
    <figure className={styles.chip}>
      {url && !failed ? (
        // eslint-disable-next-line @next/next/no-img-element -- rendered on demand by the API, sized here
        <img src={url} width={CHIP} height={CHIP} alt={`Sentinel-2 true colour of the field ${when.toLowerCase()} the burn, ${side?.date ? dateIST(`${side.date}T12:00:00+05:30`) : 'date unknown'}`} loading="lazy" decoding="async" onError={() => setFailedUrl(url)} />
      ) : (
        <span className={styles.chipMissing}>{failed ? 'The picture did not load' : 'No clear picture'}</span>
      )}
      {/* the date on one line and the cloud on the next, so the two captions line up */}
      <figcaption>
        <span>
          {when}, {side?.date ? dateIST(`${side.date}T12:00:00+05:30`) : '—'}
        </span>
        <span className={styles.faint}>{side?.cloud_pct != null ? `cloud ${percent(side.cloud_pct / 100)}` : 'cloud not stated'}</span>
      </figcaption>
    </figure>
  );
}

/**
 * The compact case summary, when a scar is clicked or chosen: over the map's right edge on a wide
 * screen, under the map on a phone (there the map stays in view with the field on it). Its head and
 * the link to the case file stay put; what lies between scrolls when the map is short, and fades at
 * the edge that has more.
 */
export function CaseSummary() {
  const { selectedScar } = useLiveState();
  const commands = useMapCommands();
  const c = useScarCaseLive(selectedScar);
  const head = useLiveHeadline();
  const ref = useRef<HTMLElement>(null);
  const cue = useScrollCue<HTMLDivElement>('y');

  useEffect(() => {
    if (selectedScar == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') commands.openCase(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedScar, commands]);

  if (selectedScar == null) return null;
  const p = c.data?.case;
  const state = p ? scarState(p) : null;
  const title = p?.label ?? (state === 'unseen' ? 'Candidate unseen burn' : state === 'alerted' ? 'Candidate burn, FIRMS-confirmed' : 'Candidate burn');
  const precision = state === 'unseen' ? precisionText(head.data?.unseen_burns) : null;
  const explaining = (c.data?.fires_nearby ?? []).filter((f) => f.explains_scar).length;
  const fireLine =
    state === 'unchecked' ? 'fire alerts not checked yet' : explaining ? `${number(explaining)} fire alert${explaining === 1 ? '' : 's'} explain it` : 'no fire alert in reach';

  return (
    <aside ref={ref} className={styles.caseCard} aria-label="Case summary" aria-live="polite">
      <header className={styles.caseHead}>
        <h3 className={styles.caseTitle}>{p ? title.charAt(0).toUpperCase() + title.slice(1) : <Skeleton width={180} height={18} />}</h3>
        <button type="button" className={styles.iconButton} aria-label="Close case summary" onClick={() => commands.openCase(null)}>
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      {c.error && !c.data ? (
        <div className={styles.caseBody}>
          <ErrorNote what="this case" error={c.error} onRetry={() => void c.mutate()} />
        </div>
      ) : !p ? (
        <div className={styles.caseBody}>
          <Skeleton width="70%" height={14} />
          <div className={styles.chips}>
            <Skeleton width={CHIP} height={CHIP + 34} />
            <Skeleton width={CHIP} height={CHIP + 34} />
          </div>
          <Skeleton width="100%" height={84} />
        </div>
      ) : (
        <>
          <div className={styles.caseBody} ref={cue}>
            <p className={styles.casePlace}>{placeName(p.village ? `Near ${p.village}` : null, p.subdistrict, p.district, p.state ?? null) || 'Place not named'}</p>
            {/* the evidence in one line, above the pictures, so a short map's card shows it unscrolled */}
            <p className={`${styles.caseFacts} nums`}>
              {[
                metricText('area_ha', p.area_ha),
                `burnt ${dateRangeIST(p.burn_after, p.burn_before)}`,
                fireLine,
                p.confidence != null ? `confidence ${number(Number(p.confidence), { decimals: 2 })}` : null,
              ]
                .filter(Boolean)
                .join(' · ')}
            </p>
            <div className={styles.chips}>
              <Chip side={c.data?.imagery?.pre ?? null} when="Before" />
              <Chip side={c.data?.imagery?.post ?? null} when="After" />
            </div>
            <dl className={styles.facts}>
              <div>
                <dt>Area</dt>
                <dd className="nums">{metricText('area_ha', p.area_ha)}</dd>
              </div>
              <div>
                <dt>Burnt between</dt>
                <dd className="nums">
                  {dateIST(p.burn_after)} and {dateIST(p.burn_before)}
                </dd>
              </div>
              <div>
                <dt>Confidence</dt>
                <dd className="nums">{p.confidence != null ? number(Number(p.confidence), { decimals: 2 }) : <span className="missing">—</span>}</dd>
              </div>
              <div>
                <dt>Fire alerts</dt>
                <dd className="nums">
                  {state === 'unchecked'
                    ? 'Not checked yet'
                    : explaining
                      ? `${number(explaining)} explain it`
                      : p.nearest_fire_m != null
                        ? `None in reach; nearest ${metricText('km', p.nearest_fire_m / 1000)}`
                        : 'None in reach'}
                </dd>
              </div>
            </dl>
            {precision ? <p className={styles.note}>Candidates like this one: {precision}.</p> : null}
          </div>
          {/* the way on to the full file stays in view however far the summary above it scrolls */}
          <div className={styles.caseFoot}>
            <Link href={`/case/${p.id}`} className={styles.caseLink}>
              Open the case file
            </Link>
          </div>
        </>
      )}
    </aside>
  );
}
