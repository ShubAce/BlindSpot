'use client';

import type { StyleSpecification } from 'maplibre-gl';
import { useCallback, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react';
import useSWR from 'swr';

import { detectionsFromFires, geoHourly, PassStrip, windowsFromOrbits } from '@/components/charts/PassStrip';
import {
  airLayers,
  BaseMap,
  firesLayer,
  geostationaryLayer,
  geostationaryPixels,
  imageryLayer,
  LAYER_LIST,
  layerLegend,
  Legend,
  paletteFor,
  riskLayers,
  satelliteLayers,
  scarsLayers,
  smokeLayers,
  smokeTimeAt,
  useAnimationClock,
  windLayer,
  type MapLayerId,
  type MapLayerInput,
} from '@/components/map';
import { basemapStyleUrl, basemapTheme, hasBasemapBoundaries, indiaCompliantStyle } from '@/components/map/basemap-style';
import { readWindField } from '@/components/map/layers/wind';
import { EmptyNote, ErrorNote, Freshness, Skeleton } from '@/components/ui/states';
import { REFERENCE_SITE } from '@/lib/aoi';
import {
  API_BASE,
  PATHS,
  type AirLatestResponse,
  type Envelope,
  type FiresResponse,
  type GeostationaryDiurnalResponse,
  type GeostationaryLatestResponse,
  type ImageryFeaturedResponse,
  type ImageryKind,
  type RiskResponse,
  type ScarsResponse,
  type SmokeForwardResponse,
  type WindFieldResponse,
} from '@/lib/api';
import { istDayKey, number, relative, startOfIstDay, timeIST } from '@/lib/format';
import { CADENCE, useApi, useNow, useOrbitsTle, usePasses, useSources } from '@/lib/live';
import { liveStatus, parseTles, predictPasses, type Satellite } from '@/lib/orbits';
import { onThemeChange, readTheme, useTheme } from '@/lib/theme';
import { liveSentence } from '@/lib/use-orbits';

type Toggles = Record<MapLayerId, boolean>;

const SLOW = 30_000;

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 border-t border-rule pt-5">
      <h2 style={{ fontSize: 16, lineHeight: '24px', fontWeight: 600, color: 'var(--text)' }}>{title}</h2>
      {children}
    </section>
  );
}

/** What one layer's data currently is: loading, failed, empty or a count, with its freshness. */
type LayerLive = { data: Partial<Envelope> | undefined; error: unknown; retry: () => void };
function LayerStatus({ live, what, count, cadenceMs, empty }: { live: LayerLive; what: string; count: number | null; cadenceMs: number; empty: string }) {
  if (live.error && !live.data) return <ErrorNote what={what} error={live.error} onRetry={live.retry} />;
  if (!live.data) return <Skeleton width={160} height={14} />;
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      {count === 0 ? <EmptyNote>{empty}</EmptyNote> : count != null ? <span className="nums">{number(count)}</span> : null}
      <Freshness resp={live.data} cadenceMs={cadenceMs} />
      {live.error ? <ErrorNote what="the refresh" error={live.error} lastGood={live.data} onRetry={live.retry} /> : null}
    </div>
  );
}

/** The parts of an SWR result a status line needs. */
const asLive = (r: { data?: Partial<Envelope>; error?: unknown; mutate: () => Promise<unknown> }): LayerLive => ({
  data: r.data,
  error: r.error,
  retry: () => void r.mutate(),
});

// ---------------------------------------------------------------------------------------------
// checks that read the page itself

// Each --mark-* token against the palette of the theme on screen (it is read again when the theme changes).
const MARK_TOKENS = ['fire', 'unseen', 'alerted', 'smoke', 'straw'] as const;
function tokenDrift(): string {
  if (typeof document === 'undefined') return 'pending';
  const theme = readTheme();
  const { MARK } = paletteFor(theme);
  const root = getComputedStyle(document.documentElement);
  const off = MARK_TOKENS.filter((k) => root.getPropertyValue(`--mark-${k}`).trim().toLowerCase() !== MARK[k].toLowerCase());
  return off.length ? `Out of sync with palette.ts (${theme}): ${off.map((k) => `--mark-${k}`).join(', ')}` : `CSS --mark-* tokens match palette.ts (${theme})`;
}

export function LabClient() {
  const [on, setOn] = useState<Toggles>(() => Object.fromEntries(LAYER_LIST.map((l) => [l.id, l.defaultOn])) as Toggles);
  const [basemap, setBasemap] = useState<'dark' | 'imagery'>('dark');
  const [imageryKind, setImageryKind] = useState<ImageryKind>('swir');
  const [tileErrors, setTileErrors] = useState(0);
  const now = useNow(1_000);
  const clock = useAnimationClock(4);
  // the map, its marks and their keys follow the site's theme, as on mission control
  const theme = useTheme();

  // data: each layer's endpoint, fetched only while its layer is on (fires always: the strip uses it)
  const fires = useApi<FiresResponse>(PATHS.fires, { hours: 24 }, { refreshInterval: CADENCE.fires, timeoutMs: SLOW });
  const scars = useApi<ScarsResponse>(on.scars ? PATHS.scars : null, {}, { refreshInterval: CADENCE.scars, timeoutMs: SLOW });
  const risk = useApi<RiskResponse>(on.risk ? PATHS.risk : null, {}, { refreshInterval: CADENCE.risk, timeoutMs: SLOW });
  const smoke = useApi<SmokeForwardResponse>(on.smoke ? PATHS.smokeForward : null, undefined, { refreshInterval: CADENCE.smoke, timeoutMs: SLOW });
  const wind = useApi<WindFieldResponse>(on.wind ? PATHS.windField : null, undefined, { refreshInterval: CADENCE.wind, timeoutMs: SLOW });
  const air = useApi<AirLatestResponse>(on.air ? PATHS.airLatest : null, undefined, { refreshInterval: CADENCE.air });
  const geo = useApi<GeostationaryLatestResponse>(on.geostationary ? PATHS.geostationaryLatest : null, undefined, { refreshInterval: CADENCE.geostationary, timeoutMs: SLOW });
  const featured = useApi<ImageryFeaturedResponse>(on.imagery ? PATHS.imageryFeatured : null, undefined, { refreshInterval: CADENCE.imagery });
  const diurnal = useApi<GeostationaryDiurnalResponse>(PATHS.geostationaryDiurnal, undefined, { refreshInterval: CADENCE.geostationary });
  const tle = useOrbitsTle();
  const sources = useSources();

  const sats: Satellite[] = useMemo(() => parseTles(tle.data), [tle.data]);
  const item = featured.data?.frames?.[0]?.item_id ?? null;

  const status = useMemo(() => (now && sats.length ? liveStatus(REFERENCE_SITE, tle.data, now) : null), [now, sats.length, tle.data]);
  // the day's predicted looks, recomputed when the elements or the IST day change
  const dayKey = now ? istDayKey(now) : null;
  const dayPasses = useMemo(() => {
    if (!dayKey || !sats.length) return [];
    const start = startOfIstDay(`${dayKey}T12:00:00+05:30`);
    return start ? predictPasses(REFERENCE_SITE, sats, start, 24) : [];
  }, [dayKey, sats]);

  const onTileError = useCallback(() => setTileErrors((n) => n + 1), []);

  // static layers: rebuilt when their data, toggles or the theme change
  const dataLayers = useMemo<MapLayerInput[]>(
    () => [
      on.imagery && item ? imageryLayer({ item, kind: imageryKind, onTileError }) : null,
      on.risk ? riskLayers(risk.data, { theme }) : null,
      on.scars ? scarsLayers(scars.data, { theme }) : null,
      on.geostationary ? geostationaryLayer(geo.data, { theme }) : null,
      on.smoke ? smokeLayers(smoke.data, { theme }) : null,
      on.wind ? windLayer(wind.data, { theme }) : null,
      on.air ? airLayers(air.data, { theme }) : null,
    ],
    [on, item, imageryKind, onTileError, risk.data, scars.data, geo.data, smoke.data, wind.data, air.data, theme],
  );
  // fires re-colour their age classes every ten minutes; satellites move every second
  const fireBucket = now ? Math.floor(now.getTime() / 600_000) : 0;
  const fireLayer = useMemo(() => (on.fires ? firesLayer(fires.data, { now: new Date(fireBucket * 600_000), theme }) : null), [on.fires, fires.data, fireBucket, theme]);
  const passOverlay = useMemo(() => {
    if (!now) return [];
    const last = dayPasses.filter((p) => p.end.getTime() < now.getTime()).pop();
    const next = dayPasses.find((p) => p.start.getTime() > now.getTime());
    return [last, next].filter((p): p is NonNullable<typeof p> => !!p);
  }, [dayPasses, now]);
  const satLayer = useMemo(() => (on.satellites ? satelliteLayers(sats, now, { passes: passOverlay, theme }) : null), [on.satellites, sats, now, passOverlay, theme]);
  const layers = useMemo(() => [...dataLayers, fireLayer, satLayer], [dataLayers, fireLayer, satLayer]);

  // self-checks
  const drift = useSyncExternalStore(onThemeChange, tokenDrift, () => 'pending');
  // the style the map is showing (BaseMap fetched it into this cache, by the same rule)
  const styleTheme = basemapTheme(basemap, theme);
  const ofm = useSWR<StyleSpecification>(basemapStyleUrl(styleTheme), null);
  const compliance = useMemo(() => {
    if (!ofm.data) return null;
    const s = indiaCompliantStyle(ofm.data, { theme: styleTheme });
    return { boundaries: hasBasemapBoundaries(s), raw: hasBasemapBoundaries(ofm.data) };
  }, [ofm.data, styleTheme]);
  const server = usePasses({ lat: REFERENCE_SITE.lat, lon: REFERENCE_SITE.lon, hours_back: 0, hours_ahead: 24 });
  const agreement = useMemo(() => {
    const api = server.data?.passes;
    if (!api || !sats.length) return null;
    const from = new Date(Date.parse(server.data?.generated_at ?? '') || 0);
    if (!from.getTime()) return null;
    const mine = predictPasses(REFERENCE_SITE, sats, from, 24);
    const rows = api.map((p) => {
      const t = Date.parse(p.peak_utc);
      const match = mine.filter((m) => m.satName === p.satellite).sort((a, b) => Math.abs(a.tca.getTime() - t) - Math.abs(b.tca.getTime() - t))[0];
      return { sat: p.satellite, at: p.peak_utc, dt: match ? (match.tca.getTime() - t) / 1000 : null };
    });
    return { rows, worst: Math.max(0, ...rows.map((r) => (r.dt == null ? Infinity : Math.abs(r.dt)))) };
  }, [server.data, sats]);

  const smokeAt = smokeTimeAt(smoke.data, clock.t);
  const geoCount = useMemo(() => geostationaryPixels(geo.data).length, [geo.data]);
  const windOk = useMemo(() => (wind.data ? !!readWindField(wind.data) : null), [wind.data]);

  const layerState: Record<MapLayerId, { live: LayerLive; count: number | null; empty: string; what: string }> = {
    fires: { live: asLive(fires), count: fires.data?.count ?? null, empty: 'No fire alerts in the last 24 hours.', what: 'fire alerts' },
    geostationary: { live: asLive(geo), count: geo.data ? geoCount : null, empty: 'No GK-2A fire pixels in the latest scan.', what: 'geostationary fire pixels' },
    scars: { live: asLive(scars), count: scars.data?.totals?.clusters ?? null, empty: 'No burn scars mapped yet.', what: 'burn scars' },
    risk: { live: asLive(risk), count: risk.data?.totals?.clusters ?? null, empty: 'No harvested, unburnt fields ranked yet.', what: 'risk fields' },
    smoke: { live: asLive(smoke), count: smoke.data?.trails?.length ?? null, empty: 'No smoke run yet.', what: 'smoke trails' },
    wind: { live: asLive(wind), count: windOk === false ? 0 : null, empty: 'The wind grid is incomplete.', what: 'the wind field' },
    air: { live: asLive(air), count: air.data?.stations?.length ?? null, empty: 'No station reported in the last hours.', what: 'air quality' },
    satellites: { live: asLive(tle), count: tle.data ? sats.length : null, empty: 'No usable orbital elements.', what: 'orbital elements' },
    imagery: { live: asLive(featured), count: featured.data ? (featured.data.frames?.length ?? 0) : null, empty: 'No Sentinel-2 scene to show yet.', what: 'Sentinel-2 scenes' },
  };

  return (
    <main data-surface="app" className="grid min-h-svh grid-cols-[360px_minmax(0,1fr)] max-[900px]:grid-cols-1">
      <aside className="flex h-svh flex-col gap-5 overflow-y-auto border-r border-border bg-surface p-6 max-[900px]:h-auto">
        <header className="flex flex-col gap-1">
          <h1>Lab</h1>
          <p style={{ color: 'var(--muted)' }}>
            Every layer reads <span className="nums">{API_BASE}</span>. Nothing here is stored in the page.
          </p>
        </header>

        <Section title={`Who is watching ${REFERENCE_SITE.name}`}>
          {tle.error && !tle.data ? (
            <ErrorNote what="orbital elements" error={tle.error} onRetry={() => void tle.mutate()} />
          ) : !status || !now ? (
            <Skeleton height={40} />
          ) : (
            <p style={{ fontSize: 15, lineHeight: '22px' }}>{liveSentence(status, now, REFERENCE_SITE.name)}</p>
          )}
        </Section>

        <Section title="Layers">
          <ul className="flex flex-col gap-4">
            {LAYER_LIST.map((l) => {
              const st = layerState[l.id];
              return (
                <li key={l.id} className="flex flex-col gap-1.5">
                  <label className="flex cursor-pointer items-center gap-2" style={{ fontWeight: 500 }}>
                    <input
                      type="checkbox"
                      checked={on[l.id]}
                      onChange={(e) => setOn((s) => ({ ...s, [l.id]: e.target.checked }))}
                      style={{ accentColor: 'var(--accent)' }}
                    />
                    {l.label}
                  </label>
                  <p style={{ color: 'var(--muted)', fontSize: 12, lineHeight: '16px' }}>{l.description}</p>
                  {on[l.id] ? (
                    <>
                      <LayerStatus live={st.live} what={st.what} count={st.count} cadenceMs={l.cadenceMs} empty={st.empty} />
                      <Legend items={layerLegend(l.id, theme)} />
                      {l.id === 'imagery' ? (
                        <div className="flex items-center gap-3" style={{ fontSize: 12 }}>
                          {(['truecolor', 'swir', 'nbr'] as ImageryKind[]).map((k) => (
                            <button key={k} type="button" className={`text-button ${imageryKind === k ? 'selected-line' : ''}`} onClick={() => setImageryKind(k)} aria-pressed={imageryKind === k}>
                              {k === 'truecolor' ? 'True colour' : k === 'swir' ? 'Short-wave infrared' : 'Burn ratio'}
                            </button>
                          ))}
                          {tileErrors ? <span style={{ color: 'var(--muted)' }}>{number(tileErrors)} tiles failed</span> : null}
                        </div>
                      ) : null}
                      {l.id === 'smoke' && smokeAt ? (
                        <div className="flex items-center gap-3" style={{ fontSize: 12 }}>
                          <button type="button" className="text-button" onClick={clock.toggle} disabled={clock.reduced}>
                            {clock.playing ? 'Pause' : 'Play'}
                          </button>
                          <span className="nums" style={{ color: 'var(--muted)' }}>
                            {clock.reduced ? 'Reduced motion: whole paths shown' : `Smoke at ${timeIST(smokeAt.at)} IST`}
                          </span>
                        </div>
                      ) : null}
                    </>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </Section>

        <Section title="Basemap">
          <div className="flex gap-4">
            {(['dark', 'imagery'] as const).map((b) => (
              <button key={b} type="button" className={`text-button ${basemap === b ? 'selected-line' : ''}`} aria-pressed={basemap === b} onClick={() => setBasemap(b)}>
                {b === 'dark' ? (theme === 'light' ? 'Light' : 'Dark') : 'Sentinel-2 cloudless 2025'}
              </button>
            ))}
          </div>
        </Section>

        <Section title="Checks">
          <ul className="flex flex-col gap-2" style={{ color: 'var(--muted)' }}>
            <li>
              {compliance == null
                ? 'Basemap: waiting for the OpenFreeMap style.'
                : compliance.boundaries
                  ? 'Basemap: a boundary layer survived the filter. Fix basemap-style.ts before showing this map.'
                  : `Basemap: no boundary layer left (the raw style had ${compliance.raw ? 'some' : 'none'}); disputed region labels filtered.`}
            </li>
            <li>{drift === 'pending' ? 'Tokens: checking.' : drift}</li>
            <li>
              {server.error && !server.data
                ? `Orbits: /api/orbits/passes did not answer, so the browser's predictions are unchecked.`
                : !agreement
                  ? 'Orbits: comparing browser and API pass times.'
                  : agreement.rows.length === 0
                    ? 'Orbits: the API predicts no pass in the next 24 hours.'
                    : `Orbits: ${agreement.rows.length} API passes; the browser's times of closest approach differ by at most ${Number.isFinite(agreement.worst) ? `${agreement.worst.toFixed(1)} s` : 'a missing pass'}.`}
            </li>
          </ul>
        </Section>

        <Section title="Sources">
          {sources.error && !sources.data ? (
            <ErrorNote what="source status" error={sources.error} onRetry={() => void sources.mutate()} />
          ) : !sources.data ? (
            <Skeleton height={80} />
          ) : sources.data.sources.length === 0 ? (
            <EmptyNote>No source has been fetched yet.</EmptyNote>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {sources.data.sources.map((s) => (
                <li key={s.key} className="flex items-baseline justify-between gap-3">
                  <span className="truncate" title={s.label}>
                    {s.label}
                  </span>
                  <span className="nums shrink-0" style={{ color: s.status === 'ok' ? 'var(--faint)' : s.status === 'error' ? 'var(--negative)' : 'var(--muted)', fontSize: 12 }}>
                    {s.status === 'never' ? 'never fetched' : `${s.status}${s.last_success_at && now ? `, ${relative(s.last_success_at, now)}` : ''}`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
      </aside>

      <div className="flex h-svh min-w-0 flex-col max-[900px]:h-auto">
        <div className="relative min-h-[420px] flex-1">
          <BaseMap layers={layers} basemap={basemap} theme={theme} label="Lab map: every BlindSpot layer over Punjab, Haryana and Delhi" />
        </div>
        <div className="border-t border-border bg-surface px-6 pb-5 pt-4">
          {tle.data || fires.data ? (
            <PassStrip now={now} windows={windowsFromOrbits(dayPasses)} detections={detectionsFromFires(fires.data)} geostationary={geoHourly(diurnal.data)} />
          ) : tle.error ? (
            <ErrorNote what="the pass strip" error={tle.error} onRetry={() => void tle.mutate()} />
          ) : (
            <Skeleton height={84} />
          )}
        </div>
      </div>
    </main>
  );
}
