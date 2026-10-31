'use client';

import 'maplibre-gl/dist/maplibre-gl.css';

import type { PickingInfo } from '@deck.gl/core';
import type { ExpressionSpecification, FilterSpecification, StyleSpecification } from 'maplibre-gl';
import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode, type Ref } from 'react';
import MapGL, { AttributionControl, Layer, NavigationControl, Source, type MapLayerMouseEvent, type MapRef } from 'react-map-gl/maplibre';
import useSWR from 'swr';

import { AOI_BBOX, MAP_MAX_BOUNDS, type BBox } from '@/lib/aoi';
import { ApiError, readBoundaries, type BoundaryLevel, type BoundarySourceSpec } from '@/lib/api';
import { useBoundaries } from '@/lib/live';

import { fallbackStyle, indiaCompliantStyle, OFM_DARK_STYLE_URL } from './basemap-style';
import { mapClock, usePrefersReducedMotion } from './clock';
import { ensureMapWorker, patchMapForDeck } from './compat';
import { DeckOverlay, type TooltipContent } from './DeckOverlay';
import { BOUNDARY_STYLE } from './palette';
import { BASE_ATTRIBUTION, describePick, LAYERS, registryIdOf } from './registry';
import { BOUNDARY_ANCHOR, isAnimated, type MapLayerInput } from './types';

export type BaseMapProps = {
  /** deck.gl layers from the factories in ./layers (null/false entries are skipped). */
  layers?: readonly MapLayerInput[];
  /** "dark" (default) or "imagery" (EOX Sentinel-2 cloudless 2025 under the labels). */
  basemap?: 'dark' | 'imagery';
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
  /** Text for screen readers: what the map shows. */
  label?: string;
  /** react-map-gl children: Marker, Popup, extra Source/Layer. */
  children?: ReactNode;
};

// MapLibre 6 needs its worker URL set before the first map exists (./compat.ts).
ensureMapWorker();

async function fetchStyle(url: string): Promise<StyleSpecification> {
  const res = await fetch(url, { signal: AbortSignal.timeout(10_000) });
  if (!res.ok) throw new Error(`basemap style: ${res.status}`);
  return (await res.json()) as StyleSpecification;
}

/** OpenFreeMap's style, fetched once per session and made India-compliant; a plain ground if the
 *  service is down, so the map never blocks on it. */
function useBasemapStyle(imagery: boolean): { style: StyleSpecification | null; fallback: boolean } {
  const { data, error } = useSWR(OFM_DARK_STYLE_URL, fetchStyle, {
    revalidateOnFocus: false,
    revalidateIfStale: false,
    revalidateOnReconnect: false,
    errorRetryCount: 2,
  });
  const style = useMemo(() => (data ? indiaCompliantStyle(data, { imagery }) : error ? fallbackStyle() : null), [data, error, imagery]);
  return { style, fallback: !data && !!error };
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

function BoundaryLines({ spec, level }: { spec: BoundarySourceSpec; level: BoundaryLevel }) {
  const sourceId = `bs-boundaries-${level}`;
  const state = level === 'state';
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
 *  palette. A plain box, label and value, no arrow. Reuse it in a custom getTooltip. */
export const TOOLTIP_STYLE: Partial<CSSStyleDeclaration> = {
  backgroundColor: 'var(--bg)',
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
 * The map every page uses: MapLibre with OpenFreeMap's dark basemap made India-compliant (its own
 * boundary layers removed, our Survey of India lines drawn from /api/geo/boundaries), framed on
 * North India and unable to pan beyond it, with deck.gl layers interleaved under the place labels.
 * Credits follow the active layers. Respects prefers-reduced-motion (animated layers stand still).
 * Size it with `className` or `style`; it fills its box.
 */
export function BaseMap({
  layers = [],
  basemap = 'dark',
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
  children,
}: BaseMapProps) {
  const { style: mapStyle, fallback } = useBasemapStyle(basemap === 'imagery');
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

  const [loaded, setLoaded] = useState(false);
  const handleLoad = useCallback((e: { target: Parameters<typeof patchMapForDeck>[0] }) => {
    patchMapForDeck(e.target);
    setLoaded(true);
    onLoad?.();
  }, [onLoad]);
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
      {mapStyle ? (
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
          reuseMaps
          onLoad={handleLoad}
          onClick={onMapClick ? handleClick : undefined}
          style={{ position: 'absolute', inset: 0 }}
        >
          {/* key: the control reads its credits once, so remount it when they change */}
          <AttributionControl key={credits.join('|')} position="bottom-right" compact customAttribution={credits} />
          {controls && interactive ? <NavigationControl position="top-right" showCompass={false} /> : null}
          {districtSpec ? <BoundaryLines spec={districtSpec} level="district" /> : null}
          {stateSpec ? <BoundaryLines spec={stateSpec} level="state" /> : null}
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
