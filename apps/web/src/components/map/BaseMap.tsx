'use client';

import 'maplibre-gl/dist/maplibre-gl.css';

import type { PickingInfo } from '@deck.gl/core';
import type { ExpressionSpecification, FilterSpecification, Map as MapLibreMap, StyleSpecification } from 'maplibre-gl';
import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode, type Ref } from 'react';
import MapGL, { AttributionControl, Layer, NavigationControl, Source, type MapLayerMouseEvent, type MapRef } from 'react-map-gl/maplibre';
import useSWR from 'swr';

import { AOI_BBOX, MAP_MAX_BOUNDS, type BBox } from '@/lib/aoi';
import { ApiError, PATHS, readBoundaries, type BoundaryLevel, type BoundarySourceSpec, type GeoBoundariesResponse } from '@/lib/api';
import { useApi, useBoundaries } from '@/lib/live';
import type { Theme } from '@/lib/theme';

import { ABROAD_LABEL_LAYER, basemapStyleUrl, basemapTheme, fallbackStyle, foreignLabelPlan, indiaCompliantStyle, labelMaskFrom } from './basemap-style';
import { mapClock, usePrefersReducedMotion } from './clock';
import { ensureMapWorker, patchMapForDeck } from './compat';
import { DeckOverlay, type TooltipContent } from './DeckOverlay';
import { paletteFor } from './palette';
import { BASE_ATTRIBUTION, describePick, LAYERS, registryIdOf } from './registry';
import { BOUNDARY_ANCHOR, isAnimated, type MapLayerInput } from './types';

export type BaseMapProps = {
  /** deck.gl layers from the factories in ./layers (null/false entries are skipped). */
  layers?: readonly MapLayerInput[];
  /** "dark" (default) or "imagery" (EOX Sentinel-2 cloudless 2025 under the labels). */
  basemap?: 'dark' | 'imagery';
  /** The colour theme of the basemap and our boundary lines (default dark). A page that follows the
   *  site's theme passes useTheme() here and the same theme to its layer factories, so marks and
   *  ground always match. The imagery view keeps the dark labels whatever the theme: there the
   *  photograph is the ground. */
  theme?: Theme;
  /** Which of our Survey of India boundaries to draw (default both). */
  boundaries?: 'state' | 'district' | 'both' | 'none';
  /** The box to frame on first load (default: Punjab, Haryana and Delhi). */
  initialBounds?: BBox;
  /** Zoom buttons (default true). */
  controls?: boolean;
  interactive?: boolean;
  /** Tooltip for picked deck.gl objects; default describes fires, scars, stations and so on. */
  getTooltip?: ((info: PickingInfo) => TooltipContent) | null;
  /** A deck.gl object was clicked. */
  onPick?: (info: PickingInfo) => void;
  /** Any click on the map, with its coordinates (e.g. "passes over this field"). */
  onMapClick?: (lngLat: { lng: number; lat: number }) => void;
  onLoad?: () => void;
  /** Credits beyond the ones the active layers bring. */
  attribution?: string[];
  mapRef?: Ref<MapRef>;
  className?: string;
  style?: CSSProperties;
  /** Basemap place labels outside the Indian states we draw: 'show' (default, as the basemap has
   *  them) or 'dim' (only cities, small, dim and in Latin script; no country or province names). */
  foreignLabels?: 'show' | 'dim';
  /** Text for screen readers: what the map shows. */
  label?: string;
  /** react-map-gl children: Marker, Popup, extra Source/Layer. */
  children?: ReactNode;
};

/** The label mask's read: all states across the map's pan range, simplified (see BaseMap). */
const MASK_QUERY = { level: 'state', bbox: MAP_MAX_BOUNDS.join(','), simplify: 0.01 } as const;
const MASK_OPTIONS = { refreshInterval: 0, revalidateOnFocus: false, revalidateIfStale: false, timeoutMs: 30_000 } as const;

// MapLibre 6 needs its worker URL set before the first map exists (./compat.ts).
ensureMapWorker();

/** Which theme each fetched style was made for. SWR's cache is keyed by the URL alone (the lab reads
 *  the dark one from it as is), and while one theme's style loads it hands back the other's. */
const STYLE_THEME = new WeakMap<StyleSpecification, Theme>();

async function fetchStyle(url: string): Promise<StyleSpecification> {
  const res = await fetch(url, { signal: AbortSignal.timeout(10_000) });
  if (!res.ok) throw new Error(`basemap style: ${res.status}`);
  const style = (await res.json()) as StyleSpecification;
  STYLE_THEME.set(style, url === basemapStyleUrl('light') ? 'light' : 'dark');
  return style;
}

const STYLE_FETCH = {
  revalidateOnFocus: false,
  revalidateIfStale: false,
  revalidateOnReconnect: false,
  errorRetryCount: 2,
} as const;

/**
 * OpenFreeMap's style for the theme, fetched once per session and made India-compliant; a plain
 * ground if the service is down, so the map never blocks on it. A theme switch must never leave the
 * map without a style (it would unmount and be built again, camera and all), so until the new
 * theme's style arrives the one on screen stays, in its own theme's colours (`keepPreviousData`).
 * Once the map has loaded, the other theme's style is fetched too, so a switch is immediate.
 */
function useBasemapStyle(
  theme: Theme,
  imagery: boolean,
  foreignLabels: 'show' | 'dim',
  loaded: boolean,
): { style: StyleSpecification | null; theme: Theme; fallback: boolean } {
  const { data, error } = useSWR(basemapStyleUrl(theme), fetchStyle, { ...STYLE_FETCH, keepPreviousData: true });
  useSWR(loaded && !imagery ? basemapStyleUrl(theme === 'light' ? 'dark' : 'light') : null, fetchStyle, STYLE_FETCH);
  const dataTheme = data ? (STYLE_THEME.get(data) ?? theme) : null;
  // the theme of the style on screen: the one asked for, unless the other is held while it loads
  const shown: Theme = dataTheme === theme || error || !dataTheme ? theme : dataTheme;
  const style = useMemo(() => {
    if (data && dataTheme === shown) return indiaCompliantStyle(data, { imagery, foreignLabels, theme: shown });
    return error ? fallbackStyle(shown) : null;
  }, [data, dataTheme, shown, error, imagery, foreignLabels]);
  return { style, theme: shown, fallback: dataTheme !== theme && !!error };
}

function collectIds(inputs: readonly MapLayerInput[], out: Set<string>): Set<string> {
  for (const x of inputs) {
    if (!x) continue;
    if (Array.isArray(x)) collectIds(x as readonly MapLayerInput[], out);
    else if (isAnimated(x)) out.add(x.id);
    else out.add((x as { id: string }).id);
  }
  return out;
}

function BoundaryLines({ spec, level, theme }: { spec: BoundarySourceSpec; level: BoundaryLevel; theme: Theme }) {
  const sourceId = `bs-boundaries-${level}`;
  const state = level === 'state';
  const { BOUNDARY_STYLE } = paletteFor(theme);
  const paint = {
    'line-color': state ? BOUNDARY_STYLE.state : BOUNDARY_STYLE.district,
    'line-width': (state
      ? ['interpolate', ['linear'], ['zoom'], 4, 0.8, 8, 1.4, 12, 2]
      : ['interpolate', ['linear'], ['zoom'], 5, 0.4, 9, 1, 12, 1.4]) as ExpressionSpecification,
  };
  // If the API mixes levels in one reply, draw only this level; otherwise draw what it sent.
  const mixed = spec.kind === 'geojson' && spec.data.features.some((f) => f.properties?.level);
  const filter: FilterSpecification | undefined = mixed ? ['==', ['get', 'level'], level] : undefined;
  if (spec.kind === 'geojson') {
    return (
      <Source id={sourceId} type="geojson" data={spec.data}>
        <Layer id={`${sourceId}-line`} type="line" beforeId={BOUNDARY_ANCHOR} minzoom={state ? 0 : 5} paint={paint} filter={filter} layout={{ 'line-join': 'round' }} />
      </Source>
    );
  }
  return (
    <Source id={sourceId} type="vector" tiles={spec.tiles} attribution={spec.attribution}>
      <Layer id={`${sourceId}-line`} type="line" source-layer={spec.layer} beforeId={BOUNDARY_ANCHOR} minzoom={state ? 0 : 5} paint={paint} layout={{ 'line-join': 'round' }} />
    </Source>
  );
}

/** deck.gl writes its own tooltip colours inline (#29323c on #a0a7b4); these override them with the
 *  palette. A plain box, label and value, no arrow, on the card surface (the map's own panels share
 *  it; on dark it is the ground's #0c0a08). Reuse it in a custom getTooltip. */
export const TOOLTIP_STYLE: Partial<CSSStyleDeclaration> = {
  backgroundColor: 'var(--surface, var(--bg))',
  color: 'var(--ink)',
  padding: '6px 8px',
  font: '12px/1.4 var(--face-text)',
  fontVariantNumeric: 'tabular-nums',
  border: '1px solid var(--border, #2b2620)',
  borderRadius: '4px',
  maxWidth: '280px',
  whiteSpace: 'pre-line',
};

const noteStyle: CSSProperties = {
  position: 'absolute',
  left: 8,
  top: 8,
  maxWidth: 'min(360px, calc(100% - 16px))',
  padding: '6px 8px',
  background: 'var(--bg)',
  color: 'var(--muted)',
  font: '12px/16px var(--face-text)',
  borderRadius: 4,
  pointerEvents: 'none',
};

/**
 * The map every page uses: MapLibre with OpenFreeMap's basemap (dark, or positron for the light
 * theme) made India-compliant (its own boundary layers removed, our Survey of India lines drawn from
 * /api/geo/boundaries), framed on North India and unable to pan beyond it, with deck.gl layers
 * interleaved under the place labels. Credits follow the active layers. Respects
 * prefers-reduced-motion (animated layers stand still). Size it with `className` or `style`; it
 * fills its box.
 *
 * A theme switch swaps the style in place (MapLibre diffs one style into the other), so the map,
 * its camera and the deck.gl overlay stay: deck.gl's layer group is a custom layer the diff leaves
 * alone and puts back before its anchor on the next `styledata`, and the sources and layers added
 * at run time (our boundaries, the page's own) add themselves again on that event.
 */
export function BaseMap({
  layers = [],
  basemap = 'dark',
  theme = 'dark',
  boundaries = 'both',
  initialBounds = AOI_BBOX,
  controls = true,
  interactive = true,
  getTooltip,
  onPick,
  onMapClick,
  onLoad,
  attribution = [],
  mapRef,
  className,
  style,
  label = 'Map of Punjab, Haryana and Delhi',
  foreignLabels = 'show',
  children,
}: BaseMapProps) {
  const imagery = basemap === 'imagery';
  const [loaded, setLoaded] = useState(false);
  const { style: mapStyle, theme: mapTheme, fallback } = useBasemapStyle(basemapTheme(basemap, theme), imagery, foreignLabels, loaded);
  const wantStates = boundaries === 'state' || boundaries === 'both';
  const wantDistricts = boundaries === 'district' || boundaries === 'both';
  const states = useBoundaries({ level: 'state' }, { isPaused: () => !wantStates });
  const districts = useBoundaries({ level: 'district' }, { isPaused: () => !wantDistricts });
  const stateSpec = useMemo(() => (wantStates ? readBoundaries(states.data) : null), [wantStates, states.data]);
  const districtSpec = useMemo(() => (wantDistricts ? readBoundaries(districts.data) : null), [wantDistricts, districts.data]);
  const boundaryError = (wantStates && !states.data && states.error) || (wantDistricts && !districts.data && !wantStates && districts.error) || null;

  const reduced = usePrefersReducedMotion();
  useEffect(() => {
    mapClock.setReduced(reduced);
  }, [reduced]);

  const [mapObj, setMapObj] = useState<MapLibreMap | null>(null);
  // Cache Components keep a page the visitor left inside <Activity>: its effects are torn down and
  // run again on the way back, but its state and DOM are kept. react-map-gl's reuseMaps then moved
  // the old map into its own container in an endless loop (the tab froze), and a new map under the
  // old `loaded` state drew layers before its style existed. So the map exists only while the page
  // is shown: unmounted when it is hidden, built fresh when it is shown again.
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const frame = requestAnimationFrame(() => setShown(true)); // next frame: no map in a hidden tab
    return () => {
      cancelAnimationFrame(frame);
      setShown(false);
      setLoaded(false);
      setMapObj(null);
    };
  }, []);
  const handleLoad = useCallback((e: { target: MapLibreMap }) => {
    const map = e.target;
    patchMapForDeck(map);
    // OpenFreeMap's dark style names a pattern its sprite lacks ("wood-pattern", woods up close):
    // MapLibre drew nothing there and warned in the console each time; a clear pixel draws the same
    // nothing, quietly
    map.setMissingStyleImageResolver((id) => {
      if (!map.hasImage(id)) map.addImage(id, { width: 1, height: 1, data: new Uint8Array(4) });
    });
    setMapObj(map);
    setLoaded(true);
    onLoad?.();
  }, [onLoad]);
  // place labels kept to India (a runtime filter on the live map, so deck.gl's interleaved layers
  // and our boundary lines are untouched; undone if the map is reused elsewhere). The style starts
  // with them held (indiaCompliantStyle's `foreignLabels`), so nothing foreign paints full size
  // while the mask loads or if it never does. The mask is its own small read: every Survey of India
  // state across the map's whole pan range (Ladakh and Gujarat included, so Leh is not styled as
  // abroad), simplified to ~1 km by the API (about 90 KB gzip, cached like the other boundaries).
  const maskStates = useApi<GeoBoundariesResponse>(foreignLabels === 'dim' ? PATHS.geoBoundaries : null, MASK_QUERY, MASK_OPTIONS);
  const labelMask = useMemo(() => {
    const spec = maskStates.data ? readBoundaries(maskStates.data) : null;
    return spec?.kind === 'geojson' ? labelMaskFrom(spec.data) : null;
  }, [maskStates.data]);
  useEffect(() => {
    if (!mapObj || !labelMask || !mapStyle) return;
    const plan = foreignLabelPlan(mapStyle.layers, labelMask, mapTheme);
    const originals = new Map(mapStyle.layers.map((l) => [l.id, 'filter' in l ? l.filter : undefined]));
    const apply = () => {
      try {
        for (const f of plan.filters) if (mapObj.getLayer(f.id)) mapObj.setFilter(f.id, f.filter as FilterSpecification);
        if (mapObj.getLayer(ABROAD_LABEL_LAYER)) mapObj.setFilter(ABROAD_LABEL_LAYER, plan.abroadFilter as FilterSpecification);
        else if (plan.abroad) mapObj.addLayer(plan.abroad, plan.beforeId && mapObj.getLayer(plan.beforeId) ? plan.beforeId : undefined);
      } catch {
        // a style still loading: the style.load listener applies it once it has
      }
    };
    apply();
    mapObj.on('style.load', apply);
    return () => {
      mapObj.off('style.load', apply);
      try {
        for (const f of plan.filters) if (mapObj.getLayer(f.id)) mapObj.setFilter(f.id, (originals.get(f.id) ?? null) as FilterSpecification | null);
        // back to the style's own state: held (the layer is the style's) or gone (it was added here)
        if (mapObj.getLayer(ABROAD_LABEL_LAYER)) {
          if (originals.has(ABROAD_LABEL_LAYER)) mapObj.setFilter(ABROAD_LABEL_LAYER, (originals.get(ABROAD_LABEL_LAYER) ?? null) as FilterSpecification | null);
          else mapObj.removeLayer(ABROAD_LABEL_LAYER);
        }
      } catch {
        // the map is already gone
      }
    };
  }, [mapObj, labelMask, mapStyle, mapTheme]);

  const handleClick = useCallback((e: MapLayerMouseEvent) => onMapClick?.({ lng: e.lngLat.lng, lat: e.lngLat.lat }), [onMapClick]);

  const tooltip = useMemo(() => {
    if (getTooltip === null) return undefined;
    if (getTooltip) return getTooltip;
    return (info: PickingInfo): TooltipContent => {
      const text = describePick(info);
      return text ? { text, className: 'map-tooltip', style: TOOLTIP_STYLE } : null;
    };
  }, [getTooltip]);

  const credits = useMemo(() => {
    const out = new Set<string>(BASE_ATTRIBUTION);
    for (const id of collectIds(layers, new Set())) {
      const reg = registryIdOf(id);
      if (reg) out.add(LAYERS[reg].attribution);
    }
    for (const a of attribution) out.add(a);
    return [...out];
  }, [layers, attribution]);

  const b = initialBounds;
  return (
    <div
      role="region"
      aria-label={label}
      className={className}
      style={{ position: 'relative', width: '100%', height: '100%', minHeight: 240, overflow: 'hidden', background: 'var(--bg)', ...style }}
    >
      {mapStyle && shown ? (
        <MapGL
          ref={mapRef}
          mapStyle={mapStyle}
          initialViewState={{ bounds: [b[0], b[1], b[2], b[3]], fitBoundsOptions: { padding: 24 } }}
          maxBounds={MAP_MAX_BOUNDS}
          minZoom={4}
          maxZoom={15}
          dragRotate={false}
          pitchWithRotate={false}
          touchPitch={false}
          maxPitch={0}
          interactive={interactive}
          attributionControl={false}
          onLoad={handleLoad}
          onClick={onMapClick ? handleClick : undefined}
          style={{ position: 'absolute', inset: 0 }}
        >
          {/* key: the control reads its credits once, so remount it when they change */}
          <AttributionControl key={credits.join('|')} position="bottom-right" compact customAttribution={credits} />
          {controls && interactive ? <NavigationControl position="top-right" showCompass={false} /> : null}
          {districtSpec ? <BoundaryLines spec={districtSpec} level="district" theme={mapTheme} /> : null}
          {stateSpec ? <BoundaryLines spec={stateSpec} level="state" theme={mapTheme} /> : null}
          {loaded ? <DeckOverlay layers={layers} getTooltip={tooltip} onClick={onPick} /> : null}
          {children}
        </MapGL>
      ) : null}
      {fallback || boundaryError ? (
        <p role="status" style={noteStyle}>
          {fallback ? 'The basemap service is not answering; boundaries and data still draw. ' : ''}
          {boundaryError ? `Boundaries could not load: ${boundaryError instanceof ApiError ? boundaryError.userMessage : 'unknown reason.'}` : ''}
        </p>
      ) : null}
    </div>
  );
}
