'use client';

import { useMemo } from 'react';

import { detectionsFromFires, PassStrip, windowsFromOrbits, type StripHourly } from '@/components/charts/PassStrip';
import { ErrorNote, Skeleton } from '@/components/ui/states';
import { istParts, number, percent } from '@/lib/format';

import { useDiurnalLive, useLiveFires, useLiveHeadline } from './data';
import styles from './styles';
import { usePassWindows, useWatch } from './watch';

const HOUR = 3_600_000;
/** The strip shows the last 17 hours and the next 7: yesterday's midday look, the evening it
 *  missed, the night look, now, and the next look. */
const BACK_H = 17;

/**
 * The signature element: a day of the fire satellites' looks at Punjab, lit where one is watching
 * and dark where none is, with the FIRMS detections of the last hours as ticks and the GK-2A
 * geostationary activity by hour beneath, so the evening burning nobody photographs shows up in
 * the dark span. Windows come from the CelesTrak elements, propagated in the browser.
 */
export function PassStripPanel() {
  const { site, sats, tle, now } = useWatch(1_000);
  const fires = useLiveFires(null);
  const diurnal = useDiurnalLive();
  const head = useLiveHeadline();

  const hourStart = now ? Math.floor(now.getTime() / HOUR) * HOUR : null;
  const from = hourStart == null ? null : hourStart - BACK_H * HOUR;
  const passes = usePassWindows(site, sats, from == null ? null : from - 2 * HOUR, 28);
  const windows = useMemo(() => windowsFromOrbits(passes), [passes]);
  const detections = useMemo(() => detectionsFromFires(fires.data), [fires.data]);
  const geo = useMemo<StripHourly[] | null>(() => {
    const h = diurnal.data?.hourly_ist;
    if (!h?.length) return null;
    return h.filter((b) => b.mean_count != null && Number.isFinite(b.mean_count)).map((b) => ({ hour_ist: b.hour, value: b.mean_count as number }));
  }, [diurnal.data]);
  const startHour = from == null ? 0 : (istParts(from)?.hour ?? 0);

  const place = site?.place ?? 'Punjab';
  const g = head.data?.gk2a_evening;
  const span = g?.unobserved_span_ist;

  return (
    <section className={`${styles.card} ${styles.stripCard}`} aria-labelledby="strip-title">
      <header className={styles.stripHead}>
        <div className={styles.stripLead}>
          <h2 id="strip-title" className={styles.cardTitle}>
            Who is watching {place}
          </h2>
        </div>
        <ul className={styles.stripKey} aria-label="Key">
          <li>
            <span className={styles.keyWindow} aria-hidden="true" />
            A fire satellite looking
          </li>
          <li>
            <span className={styles.keyBlind} aria-hidden="true" />
            No fire satellite looking
          </li>
          <li>
            <span className={styles.keyTick} aria-hidden="true" />
            FIRMS detections, last 24 h
          </li>
          {geo ? (
            <li>
              <span className={styles.keyGeo} aria-hidden="true" />
              GK-2A activity, mean of {number(diurnal.data?.days ?? null)} days
            </li>
          ) : null}
        </ul>
      </header>

      <div className={styles.stripBody}>
        {tle.error && !tle.data ? (
          <ErrorNote what="orbital elements" error={tle.error} onRetry={() => void tle.mutate()} />
        ) : (
          <PassStrip
            windows={windows}
            detections={detections}
            geostationary={geo}
            geoLabel="GK-2A, mean fire pixels per scan by hour"
            geoUnit="mean fire pixels per scan"
            now={now}
            day={from ?? undefined}
            startHour={startHour}
          />
        )}
      </div>

      <p className={styles.stripNote}>
        {g?.available && g.share_outside_polar_windows != null ? (
          <>
            <strong className="nums">{percent(g.share_outside_polar_windows)}</strong> of the GK-2A fire activity of the last {number(g.days ?? null)} days fell while no polar fire satellite
            was looking{g.peak_ist ? <>; it peaked at <span className="nums">{g.peak_ist}</span> IST</> : null}
            {span ? (
              <>
                , and none looked from <span className="nums">{span.start}</span> to <span className="nums">{span.end}</span>
              </>
            ) : null}
            . An activity index in pixel-hours, not a count of fires.
          </>
        ) : g && g.available === false ? (
          <span className={styles.faint}>GK-2A geostationary activity is not available: {g.reason ?? 'no scans stored yet'}.</span>
        ) : diurnal.error && !diurnal.data ? (
          <ErrorNote what="the geostationary activity" error={diurnal.error} onRetry={() => void diurnal.mutate()} />
        ) : (
          <Skeleton width="80%" height={14} />
        )}
      </p>
    </section>
  );
}
