'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';

import { EmptyNote, ErrorNote, Freshness, Skeleton } from '@/components/ui/states';
import { duration, number, percent, timeIST } from '@/lib/format';

import { verdictText, verdictWarns } from './air-verdict';
import { useAirLive, useLiveHeadline, useSmokeReceptors, useSourceCadence } from './data';
import styles from './styles';
import { useMapCommands } from './map-commands';
import type { AirReceptorLive, SmokeReceptorLive } from './types';

/**
 * Air at the receptor cities: Delhi first, then the cities the smoke model follows. Measured PM2.5
 * is the median of CPCB stations (via OpenAQ, which republishes them 2-4 h late), so a "now" value is
 * often missing: the last value is shown with its age. The CAMS figure is model output and is set
 * apart as such. The trajectory badge and the fire-exposure index come from the back-trajectory run.
 *
 * The badge names what the run found, never where the city's smoke came from: back-trajectories from
 * the city crossing Punjab and Haryana's burning area, which says nothing of how much of its PM2.5
 * is smoke (the smoke model tracks measured PM2.5 less well than CAMS, /method#smoke). So the words
 * are the trajectories' own ("Air crossed the burn belt"), in the neutral style, and the warning
 * colour is kept for a crossing city whose PM2.5 is also above 60 µg/m³ (the CPCB 'satisfactory'
 * limit). The landing's Delhi line uses the same words and the same rule (air-verdict.ts).
 */

const FIRST = 3;

/** "46 min old", "2 h old": an age short enough for a table cell; the exact time is in the title. */
const ageShort = (min: number) => (min < 60 ? `${Math.round(min)} min old` : `${Math.floor(min / 60)} h old`);

/** FEI spans orders of magnitude between cities: two decimals when small, whole when large. */
const fei = (v: number | null | undefined) => (v == null ? '—' : number(v, { decimals: Math.abs(v) < 10 ? 2 : 0 }));


function Verdict({ hint, pm25 }: { hint: string | null | undefined; pm25: number | null }) {
  if (!hint) return <span className="missing">No trajectory run</span>;
  const tone = verdictWarns(hint, pm25) ? 'warning' : 'neutral';
  return (
    <span className={styles.pill} data-tone={tone}>
      {verdictText(hint)}
    </span>
  );
}

type Row = { city: string; air: AirReceptorLive | null; smoke: SmokeReceptorLive | null };

/** "66 µg/m³", "measured 2 h 26 min ago" or "now", from whichever of now/last the API had. */
function measured(a: AirReceptorLive | null): { value: number | null; age: number | null; now: boolean; stations: number | null } {
  if (!a) return { value: null, age: null, now: false, stations: null };
  if (a.pm25_now != null) return { value: a.pm25_now, age: null, now: true, stations: a.n_stations };
  const l = a.pm25_last;
  return { value: l?.value ?? null, age: l?.age_minutes ?? null, now: false, stations: l?.n_stations ?? null };
}

export function AirReceptors() {
  const air = useAirLive();
  const smoke = useSmokeReceptors();
  const head = useLiveHeadline();
  const commands = useMapCommands();
  const cadence = useSourceCadence();
  const [all, setAll] = useState(false);

  const rows = useMemo<Row[]>(() => {
    const airBy = new Map((air.data?.receptors ?? []).map((r) => [r.city.toLowerCase(), r]));
    const smokeBy = new Map((smoke.data?.receptors ?? []).map((r) => [r.city.toLowerCase(), r]));
    // the cities the smoke model follows, plus Delhi; most exposed first after Delhi
    const cities = new Set<string>(['delhi', ...smokeBy.keys()]);
    return [...cities]
      .map((c) => ({ city: smokeBy.get(c)?.city ?? airBy.get(c)?.city ?? c, air: airBy.get(c) ?? null, smoke: smokeBy.get(c) ?? null }))
      .filter((r) => r.air || r.smoke)
      .sort((a, b) => (a.city === 'Delhi' ? -1 : b.city === 'Delhi' ? 1 : (b.smoke?.fire_exposure_index ?? -1) - (a.smoke?.fire_exposure_index ?? -1)));
  }, [air.data, smoke.data]);

  const delhi = rows.find((r) => r.city === 'Delhi') ?? null;
  const others = rows.filter((r) => r !== delhi);
  const trace = head.data?.delhi?.smoke_trace ?? null;
  const dm = measured(delhi?.air ?? null);
  const delhiSmoke = delhi?.smoke;
  const reason = delhiSmoke?.verdict_reason ?? trace?.verdict_reason ?? null;
  const belt24 = (trace?.share_over_burn_belt ?? delhiSmoke?.share_over_burn_belt)?.h24 ?? null;
  const exposure = trace?.fire_exposure_index ?? delhiSmoke?.fire_exposure_index;
  const range = trace?.fire_exposure_range ?? delhiSmoke?.fire_exposure_range ?? null;
  const loading = !air.data && !air.error;

  return (
    <section className={styles.card} aria-labelledby="air-title">
      <header className={styles.cardHead}>
        <h2 id="air-title" className={styles.cardTitle}>
          Air at receptors
        </h2>
        {air.data ? <Freshness resp={air.data} cadenceMs={cadence('openaq')} /> : null}
      </header>

      {air.error && !air.data ? (
        <div className={styles.cardPad}>
          <ErrorNote what="air quality" error={air.error} onRetry={() => void air.mutate()} />
        </div>
      ) : (
        <>
          <div className={styles.delhi}>
            <div className={styles.delhiTop}>
              <h3 className={styles.delhiName}>
                <button type="button" className={styles.linkish} onClick={() => commands.showLayers(['smokeBack'], { city: 'Delhi' })}>
                  Delhi
                </button>
              </h3>
              {smoke.data || head.data ? <Verdict hint={delhi?.smoke?.verdict_hint ?? trace?.verdict_hint} pm25={dm.value} /> : <Skeleton width={120} height={20} />}
            </div>
            <div className={styles.delhiFigures}>
              <div>
                <p className={styles.figLabel}>PM2.5, measured</p>
                {loading ? (
                  <Skeleton width={90} height={24} />
                ) : dm.value != null ? (
                  <p className={styles.fig}>
                    <span className="nums">{number(dm.value)}</span> <span className={styles.unit}>µg/m³</span>
                  </p>
                ) : (
                  <p className={styles.fig}>
                    <span className="missing">—</span>
                  </p>
                )}
                <p className={styles.figSub}>
                  {loading ? null : dm.now ? `now, median of ${number(dm.stations)} stations` : dm.age != null ? `last value, ${duration(dm.age * 60_000)} old${dm.stations ? `, ${number(dm.stations)} stations` : ''}` : 'no station has reported'}
                </p>
              </div>
              <div>
                <p className={styles.figLabel}>Model (CAMS)</p>
                {loading ? (
                  <Skeleton width={70} height={20} />
                ) : (
                  <p className={styles.figModel}>
                    {delhi?.air?.model?.pm25 != null ? (
                      <>
                        <span className="nums">{number(delhi.air.model.pm25)}</span> <span className={styles.unit}>µg/m³</span>
                      </>
                    ) : (
                      <span className="missing">—</span>
                    )}
                  </p>
                )}
                <p className={styles.figSub}>not measured{delhi?.air?.model?.valid_at ? `; for ${timeIST(delhi.air.model.valid_at)} IST` : ''}</p>
              </div>
            </div>
            {trace || delhi?.smoke ? (
              // the run's own reason, in view (a tooltip never shows on a touch screen), then its figures
              <p className={styles.delhiNote}>
                {reason ? <>Back-trajectories from Delhi: {reason}. </> : null}
                {belt24 != null ? <>The air spent <span className="nums">{percent(belt24)}</span> of its last 24 h over the belt; fire </> : 'Fire '}
                exposure <span className="nums">{fei(exposure)}</span>
                {range ? (
                  <span className={styles.faint}>
                    {' '}(models {fei(range[0])}–{fei(range[1])})
                  </span>
                ) : null}
                . This does not measure how much of its PM2.5 is smoke.
              </p>
            ) : smoke.data && head.data ? (
              <EmptyNote>No back-trajectory run for Delhi yet.</EmptyNote>
            ) : null}
          </div>

          {smoke.error && !smoke.data ? (
            <div className={styles.cardPad}>
              <ErrorNote what="the smoke verdicts" error={smoke.error} onRetry={() => void smoke.mutate()} />
            </div>
          ) : null}

          <table className={styles.airTable}>
            <caption className="sr-only">Other receptor cities, highest fire exposure first</caption>
            <thead>
              <tr>
                <th scope="col">City</th>
                <th scope="col" className="num">
                  <abbr title="Measured PM2.5 in µg/m³, median of CPCB stations via OpenAQ">PM2.5</abbr>
                </th>
                <th scope="col" className="num">
                  <abbr title="Copernicus CAMS model PM2.5, not a measurement">Model</abbr>
                </th>
                <th scope="col" className="num">
                  <abbr title="Fire-exposure index from the back-trajectory run, defined under the table">Exposure</abbr>
                </th>
              </tr>
            </thead>
            <tbody>
              {loading || (!smoke.data && !smoke.error)
                ? Array.from({ length: FIRST }, (_, i) => (
                    <tr key={i}>
                      <td colSpan={4}>
                        <Skeleton width="100%" height={14} />
                      </td>
                    </tr>
                  ))
                : (all ? others : others.slice(0, FIRST)).map((r) => {
                    const m = measured(r.air);
                    return (
                      <tr key={r.city}>
                        <th scope="row">
                          <span className={styles.cityName}>{r.city}</span>
                          <span className={styles.citySub}>
                            {r.smoke?.verdict_hint ? verdictText(r.smoke.verdict_hint) : 'no trajectory run'}
                          </span>
                        </th>
                        <td className="num">
                          {m.value != null ? (
                            <>
                              <span>{number(m.value)}</span>
                              <span className={styles.citySub} title={m.age != null ? `${duration(m.age * 60_000)} old` : undefined}>
                                {m.now ? 'now' : m.age != null ? ageShort(m.age) : ''}
                              </span>
                            </>
                          ) : (
                            <span className="missing">—</span>
                          )}
                        </td>
                        <td className="num">{r.air?.model?.pm25 != null ? <span className={styles.faint}>{number(r.air.model.pm25)}</span> : <span className="missing">—</span>}</td>
                        <td className="num">{fei(r.smoke?.fire_exposure_index)}</td>
                      </tr>
                    );
                  })}
            </tbody>
          </table>
          {others.length > FIRST ? (
            <div className={styles.listFoot}>
              <button type="button" className="text-button" onClick={() => setAll((v) => !v)}>
                {all ? 'Show fewer' : `Show all ${number(others.length)} cities`}
              </button>
            </div>
          ) : null}
          <p className={`${styles.footNote} ${styles.airDefine}`}>
            Fire exposure is a model index: the tonnes of crop residue burning near the paths the model traces back from each city, each weighted by the share of its air that
            passed as it burned. It measures exposure, not the smoke in the city’s air. <Link href="/method#smoke">How the smoke model is checked</Link>
          </p>
        </>
      )}
    </section>
  );
}
