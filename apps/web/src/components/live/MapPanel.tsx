'use client';

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';

import { LegendSwatch } from '@/components/map/Legend';
import { lookOffsetText, nextLookParts } from '@/components/map/look';
import { EmptyNote, ErrorNote, Freshness, Skeleton } from '@/components/ui/states';
import { PATHS, type ApiError, type Envelope, type RiskResponse, type SmokeForwardResponse, type WindFieldResponse } from '@/lib/api';
import { dateIST, metricText, number, timeIST } from '@/lib/format';
import { CADENCE, useApi } from '@/lib/live';

import { CaseSummary } from './CaseSummary';
import { SCAR_QUERY, SMOKE_FORWARD_QUERY, useAirLive, useDistrictScars, useLiveFires, useLiveScars, useScarCaseLive, useScarsSummaryLive, useSmokeBackLive, useSourceCadence } from './data';
import { layerKey, levelText, scarTileKey, toggleSwatch, type LayerKeyMeta } from './legend';
import { LIVE_LAYERS, useLiveActions, useLiveState, type LiveLayer } from './live-state';
import type { MapData } from './LiveMapCanvas';
import styles from './styles';
import { MotionToggle } from './MotionToggle';
import { SeasonReplay } from './SeasonReplay';
import { usePassWindows, useWatch } from './watch';

/** MapLibre and deck.gl arrive in their own chunk, after the rest of the screen has painted. */
const LiveMapCanvas = dynamic(() => import('./LiveMapCanvas'), {
  ssr: false,
  loading: () => <MapWaiting />,
});

function MapWaiting() {
  return (
    <div className={styles.mapWaiting} role="status">
      <span>Loading the map…</span>
    </div>
  );
}

/**
 * The map's chunk (MapLibre and deck.gl, ~1.8 MB) is asked for only once the browser is idle after
 * the screen has hydrated, so on a slow phone the finding, the list and the strip paint and answer
 * first instead of waiting behind it. A second at most.
 */
function useWhenIdle(): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const go = () => setReady(true);
    if (typeof window.requestIdleCallback === 'function') {
      const id = window.requestIdleCallback(go, { timeout: 1_000 });
      return () => window.cancelIdleCallback(id);
    }
    const t = setTimeout(go, 200);
    return () => clearTimeout(t);
  }, []);
  return ready;
}

const SLOW = 30_000;

/** Room for the key beside the data: a wide screen tall enough that the map body passes ~450px. */
const WIDE = '(min-width: 1101px) and (min-height: 1000px)';
const subscribeWide = (cb: () => void) => {
  const m = window.matchMedia(WIDE);
  m.addEventListener('change', cb);
  return () => m.removeEventListener('change', cb);
};
const isWide = () => window.matchMedia(WIDE).matches;
/** Air stations older than this are not drawn as now (components/map/layers/air.ts). */
const AIR_MAX_AGE_MIN = 360;

type Status = { live: { data?: Partial<Envelope>; error?: ApiError; mutate: () => unknown }; what: string; line: ReactNode | null; empty: string | null; cadenceMs: number | undefined };

function LayerStatus({ st }: { st: Status }) {
  const { data, error, mutate } = st.live;
  if (error && !data) return <ErrorNote what={st.what} error={error} onRetry={() => void mutate()} />;
  if (!data) return <Skeleton width={150} height={12} />;
  return (
    <p className={styles.keyStatus}>
      {st.empty ? <EmptyNote style={{ display: 'inline' }}>{st.empty}</EmptyNote> : st.line}
      {' '}
      <Freshness resp={data} cadenceMs={st.cadenceMs} />
      {error ? <ErrorNote what="the latest refresh" error={error} lastGood={data} onRetry={() => void mutate()} /> : null}
    </p>
  );
}

/** The tiles' colour steps, highest share first, under what they measure. */
function TileRamp({ view }: { view: 'districts' | 'tiles' }) {
  const k = scarTileKey(view);
  return (
    <figure className={styles.keyRamp}>
      <figcaption>{k.rampTitle}</figcaption>
      <ul className={styles.keyList}>
        {k.ramp
          .slice()
          .reverse()
          .map((it) => (
            <li key={it.label}>
              <LegendSwatch mark={it.mark} />
              <span>{it.label}</span>
            </li>
          ))}
      </ul>
    </figure>
  );
}

export function MapPanel() {
  const s = useLiveState();
  const { dispatch } = useLiveActions();
  const on = s.layers;
  // the key starts open where there is room for it (wide and tall screens) and folded elsewhere, so it
  // never covers half of a short map; its button stays in the map's corner
  const [keyChoice, setKeyChoice] = useState<boolean | null>(null);
  const wide = useSyncExternalStore(subscribeWide, isWide, () => false);
  // a case summary folds the key out of its way (the two would cover the map between them) until
  // the viewer opens it again
  const keyOpen = keyChoice ?? (wide && s.selectedScar == null);
  const mapReady = useWhenIdle();

  // data: the heavy layers load only while switched on
  const fires = useLiveFires(s.replayDay);
  const scars = useLiveScars(s.replayDay);
  const districtScars = useDistrictScars(s.scarDistrict, s.replayDay);
  const summary = useScarsSummaryLive();
  const risk = useApi<RiskResponse>(on.risk ? PATHS.risk : null, { limit: 1000 }, { refreshInterval: CADENCE.risk, timeoutMs: SLOW });
  const smoke = useApi<SmokeForwardResponse>(on.smoke ? PATHS.smokeForward : null, SMOKE_FORWARD_QUERY, { refreshInterval: CADENCE.smoke, timeoutMs: 60_000 });
  const back = useSmokeBackLive(on.smokeBack ? s.smokeBackCity : null);
  const wind = useApi<WindFieldResponse>(on.wind ? PATHS.windField : null, undefined, { refreshInterval: CADENCE.wind, timeoutMs: SLOW });
  const air = useAirLive();
  const selected = useScarCaseLive(s.selectedScar);
  const cadence = useSourceCadence();

  const { site, sats, tle, now, status } = useWatch(1_000);
  // the footprint of the next look at the site, labelled with its IST time
  const bucket = now ? Math.floor(now.getTime() / 600_000) * 600_000 : null;
  const around = usePassWindows(site, sats, bucket == null ? null : bucket - 12 * 3600_000, 36);
  const passes = useMemo(() => {
    if (!status) return [];
    return around.filter((p) => status.next && p.tca.getTime() === status.next.at.getTime());
  }, [around, status]);

  // where the next look's track lies from the site, for the key (the map draws the same geometry)
  const look = useMemo(() => (on.satellites && site && passes[0] ? nextLookParts(sats, passes[0], site) : null), [on.satellites, site, passes, sats]);

  // the map draws the newest scars overall plus, once a district was clicked, that district's newest
  // (the list and the counts read the overall list alone)
  const drawnScars = useMemo(() => {
    const a = scars.data;
    const b = districtScars.data;
    if (!a || !b) return a ?? b;
    const seen = new Set(a.features.features.map((f) => f.properties.id));
    const extra = b.features.features.filter((f) => !seen.has(f.properties.id));
    return extra.length ? { ...a, features: { ...a.features, features: [...a.features.features, ...extra] } } : a;
  }, [scars.data, districtScars.data]);

  const data: MapData = {
    fires: fires.data,
    scars: drawnScars,
    risk: risk.data,
    smoke: smoke.data,
    smokeBack: back.data,
    wind: wind.data,
    air: air.data,
    sats,
    passes,
    site,
    now,
    cases: selected.data ? [selected.data] : [],
  };

  // Measured stations and CAMS model points are drawn alike (models as rings) but never counted
  // together: a model's output is not a station that reported.
  const airDrawn = useMemo(() => {
    const drawn = (air.data?.stations ?? []).filter((st) => st.pm25 != null && !(st.age_minutes != null && st.age_minutes > AIR_MAX_AGE_MIN));
    const models = drawn.filter((st) => st.is_model).length;
    return { stations: drawn.length - models, models };
  }, [air.data]);
  const dayLabel = s.replayDay ? dateIST(`${s.replayDay}T12:00:00+05:30`) : null;

  const statuses: Record<LiveLayer, Status> = {
    fires: {
      live: fires,
      what: 'fire alerts',
      line: fires.data ? `${number(fires.data.count)} detections ${dayLabel ? `on ${dayLabel}` : 'in the last 24 h'}` : null,
      empty: fires.data && fires.data.count === 0 ? (dayLabel ? `No fire alerts on ${dayLabel}.` : 'No fire alerts in the last 24 hours.') : null,
      cadenceMs: cadence('firms'),
    },
    scars: {
      live: scars,
      what: 'burn scars',
      line: scars.data
        ? [
            s.scarsView === 'districts' && summary.data?.season
              ? `This season by district: ${metricText('area_ha', summary.data.season.area_ha)} in ${number(summary.data.by_district?.length ?? 0)} districts, since ${dateIST(`${summary.data.season.since}T12:00:00+05:30`)}.`
              : null,
            `${s.scarsView === 'districts' ? 'Zoomed in, the fields: newest' : 'Newest'} ${number(scars.data.features.features.length)} of ${number(scars.data.totals.clusters)} scars with confidence ${number(SCAR_QUERY.min_confidence, { decimals: 1 })} or more${dayLabel ? `, up to ${dayLabel}` : ''}${
              districtScars.data && s.scarDistrict && !dayLabel ? `, and newest ${number(districtScars.data.features.features.length)} of ${number(districtScars.data.totals.clusters)} in ${s.scarDistrict}` : ''
            }`,
          ]
            .filter(Boolean)
            .join(' ')
        : null,
      empty: scars.data && scars.data.totals.clusters === 0 ? (dayLabel ? `No Sentinel-2 burn scars mapped up to ${dayLabel}.` : 'No burn scars mapped yet.') : null,
      cadenceMs: cadence('stac_e84'),
    },
    risk: {
      live: risk,
      what: 'harvested fields',
      line: risk.data ? `Top ${number(risk.data.features.features.length)} of ${number(risk.data.totals.clusters)} harvested, unburnt fields by risk` : null,
      empty: risk.data && risk.data.features.features.length === 0 ? 'No harvested, unburnt fields ranked yet.' : null,
      cadenceMs: cadence('stac_e84'),
    },
    smoke: {
      live: smoke,
      what: 'the smoke forecast',
      line: smoke.data ? `${number(smoke.data.trails?.length ?? 0)} trails from ${number(smoke.data.inputs?.fires ?? 0)} fires` : null,
      empty: smoke.data && !smoke.data.trails?.length ? 'No smoke run yet.' : null,
      cadenceMs: cadence('openmeteo'),
    },
    smokeBack: {
      live: back,
      what: `the air reaching ${s.smokeBackCity}`,
      line: back.data ? `${number(back.data.trails?.length ?? 0)} back trajectories from ${back.data.city}` : null,
      empty: back.data && !back.data.trails?.length ? `No back-trajectory run for ${s.smokeBackCity} yet.` : null,
      cadenceMs: cadence('openmeteo'),
    },
    wind: {
      live: wind,
      what: 'the wind field',
      line: wind.data ? (levelText(wind.data.level) ? `Model wind, ${levelText(wind.data.level)}` : 'Model wind') : null,
      empty: wind.data && !(wind.data.u as unknown[] | undefined)?.length ? 'The wind grid is empty.' : null,
      cadenceMs: cadence('openmeteo'),
    },
    air: {
      live: air,
      what: 'air quality',
      line: air.data
        ? `${number(airDrawn.stations)} stations reported in the last 6 h${airDrawn.models ? `; ${number(airDrawn.models)} CAMS model points (rings) are estimates, not measurements` : ''}`
        : null,
      empty: air.data && airDrawn.stations === 0 ? 'No station has reported in the last 6 h; their last readings are listed under Air at receptors.' : null,
      cadenceMs: cadence('openaq'),
    },
    satellites: {
      live: tle,
      what: 'orbital elements',
      line: tle.data
        ? `${number(sats.length)} satellites from CelesTrak elements.${look && passes[0] ? ` Next look: ${passes[0].satName} at ${timeIST(passes[0].tca)} IST, ${lookOffsetText(look)}.` : ''}`
        : null,
      empty: tle.data && sats.length === 0 ? 'No usable orbital elements.' : null,
      cadenceMs: cadence('celestrak'),
    },
  };

  // the key and MapLibre's credits share the map's lower edge: opening one folds the other
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const onClick = (e: MouseEvent) => {
      if (!(e.target instanceof Element) || !e.target.closest('.maplibregl-ctrl-attrib-button')) return;
      requestAnimationFrame(() => {
        if (body.querySelector('.maplibregl-compact-show')) setKeyChoice(false);
      });
    };
    body.addEventListener('click', onClick);
    // pause motion sits above the credits: follow the credits' corner as it folds and unfolds
    let ro: ResizeObserver | null = null;
    let seen: Element | null = null;
    const watch = () => {
      const corner = body.querySelector('.maplibregl-ctrl-bottom-right');
      if (!corner || corner === seen) return;
      ro?.disconnect();
      seen = corner;
      ro = new ResizeObserver(() => body.style.setProperty('--lv-credits-h', `${Math.round(corner.getBoundingClientRect().height)}px`));
      ro.observe(corner);
    };
    const mo = new MutationObserver(watch);
    mo.observe(body, { childList: true, subtree: true });
    watch();
    return () => {
      body.removeEventListener('click', onClick);
      mo.disconnect();
      ro?.disconnect();
    };
  }, []);
  const toggleKey = () => {
    if (!keyOpen) bodyRef.current?.querySelector('.maplibregl-compact-show')?.classList.remove('maplibregl-compact-show');
    setKeyChoice(!keyOpen);
  };

  const active = LIVE_LAYERS.filter((l) => on[l]);
  const keyMeta: LayerKeyMeta = { windLevel: wind.data?.level, backHours: back.data?.hours };
  const mapLabel = `Map of Punjab, Haryana and Delhi showing ${active.map((l) => layerKey(l, s.smokeBackCity, keyMeta).label.toLowerCase()).join(', ') || 'no layers'}${dayLabel ? `, replaying ${dayLabel}` : ''}`;

  return (
    <section className={`${styles.card} ${styles.mapCard}`} aria-labelledby="map-title">
      <header className={styles.mapHead}>
        <h2 id="map-title" className="sr-only">
          Map
        </h2>
        <div role="toolbar" aria-label="Map layers" className={styles.toggles}>
          {LIVE_LAYERS.map((l) => {
            const k = layerKey(l, s.smokeBackCity, keyMeta);
            const st = statuses[l];
            const busy = on[l] && !st.live.data && !st.live.error;
            return (
              <button
                key={l}
                type="button"
                className={styles.toggle}
                aria-pressed={on[l]}
                aria-busy={busy || undefined}
                onClick={() => dispatch({ type: 'toggle', layer: l })}
              >
                <span className={styles.toggleSwatch} aria-hidden="true">
                  <LegendSwatch mark={toggleSwatch(l)} />
                </span>
                <span className={on[l] ? `${styles.toggleLabel} selected-line` : styles.toggleLabel}>{k.label}</span>
              </button>
            );
          })}
        </div>
      </header>

      <div className={styles.mapBody} ref={bodyRef}>
        {mapReady ? <LiveMapCanvas data={data} label={mapLabel} /> : <MapWaiting />}

        {s.replayDay ? (
          <p className={styles.replayChip} role="status">
            Replaying {dayLabel}: fire alerts and scars of that day; other layers show now.{' '}
            <button type="button" className="text-button" onClick={() => dispatch({ type: 'replay', day: null })}>
              Back to live
            </button>
          </p>
        ) : null}

        <div className={styles.key} data-state={keyOpen ? 'open' : 'closed'}>
          <button type="button" className={styles.keyButton} aria-expanded={keyOpen} aria-controls="map-key" onClick={toggleKey}>
            Key
          </button>
          <div id="map-key" className={styles.keyBody}>
            {active.length ? (
              active.map((l) => {
                const k = layerKey(l, s.smokeBackCity, keyMeta);
                return (
                  <section key={l} className={styles.keyGroup} aria-label={k.label}>
                    <h3 className={styles.keyTitle}>{k.label}</h3>
                    <LayerStatus st={statuses[l]} />
                    {l === 'scars' && s.scarsView !== 'fields' ? <TileRamp view={s.scarsView} /> : null}
                    <ul className={styles.keyList}>
                      {(l === 'scars' && s.scarsView !== 'fields' ? scarTileKey(s.scarsView).legend : k.legend).map((it) => (
                        <li key={it.label}>
                          <LegendSwatch mark={it.mark} />
                          <span>{it.label}</span>
                        </li>
                      ))}
                    </ul>
                  </section>
                );
              })
            ) : (
              <EmptyNote>Every layer is off. Switch one on above the map.</EmptyNote>
            )}
          </div>
        </div>

        {on.smoke || on.smokeBack || on.wind ? (
          <div className={styles.mapHeadEnd}>
            <MotionToggle />
          </div>
        ) : null}

        <CaseSummary />
      </div>

      <SeasonReplay />
    </section>
  );
}
