'use client';

import { createContext, use, useCallback, useMemo, useReducer, useRef, type ReactNode, type RefObject } from 'react';
import type { MapRef } from 'react-map-gl/maplibre';

import { flyTo as cameraFlyTo, type CameraTarget } from '@/components/map/camera';

/**
 * Mission control's shared state: which layers are on, the replay day, the scar being read, what is
 * highlighted, and the camera. One reducer, two contexts: `LiveStateContext` (values, re-renders
 * readers) and `LiveActionsContext` (stable functions, never re-renders). The map, the ranked list,
 * the replay and the investigator console all act through the same actions, so a click in the list
 * and a `map` event from the agent move the map the same way. Components use `useLiveState()` and
 * `useMapCommands()` (./map-commands.tsx); nothing else touches the reducer.
 */

export type LiveLayer = 'fires' | 'scars' | 'risk' | 'smoke' | 'smokeBack' | 'wind' | 'air' | 'satellites';

/** Draw order of the toggles (left to right), the order the legend lists them in. */
export const LIVE_LAYERS: readonly LiveLayer[] = ['fires', 'scars', 'risk', 'smoke', 'smokeBack', 'wind', 'air', 'satellites'];

export type HighlightKind = 'scar' | 'risk_cluster' | 'fire' | 'station';
export type PlaceMark = { lon: number; lat: number; label?: string | null };

export type LiveState = {
  layers: Record<LiveLayer, boolean>;
  /** IST day being replayed ("2026-10-24"), or null for live. */
  replayDay: string | null;
  /** The scar whose case summary is open. */
  selectedScar: number | null;
  /** The scar under the pointer in the ranked list (outlined on the map). */
  hoverScar: number | null;
  /** Ids per kind, as strings (the API mixes numeric and "openaq:6964" ids). */
  highlights: Record<HighlightKind, string[]>;
  /** Places the console pointed at (a district, a city, a village or a point). */
  places: PlaceMark[];
  /** When set, the map frames the highlighted features once their geometry is known. */
  focusHighlights: number;
  /** City for the back trajectories (smoke reaching a city). */
  smokeBackCity: string;
};

export const DEFAULT_LAYERS: Record<LiveLayer, boolean> = {
  fires: true,
  scars: true,
  risk: false,
  smoke: false,
  smokeBack: false,
  wind: true,
  air: false,
  satellites: true,
};

const EMPTY_HIGHLIGHTS: Record<HighlightKind, string[]> = { scar: [], risk_cluster: [], fire: [], station: [] };

const initialState: LiveState = {
  layers: DEFAULT_LAYERS,
  replayDay: null,
  selectedScar: null,
  hoverScar: null,
  highlights: EMPTY_HIGHLIGHTS,
  places: [],
  focusHighlights: 0,
  smokeBackCity: 'Delhi',
};

type Action =
  | { type: 'layers'; on?: readonly LiveLayer[]; off?: readonly LiveLayer[]; only?: boolean }
  | { type: 'toggle'; layer: LiveLayer }
  | { type: 'replay'; day: string | null }
  | { type: 'select'; id: number | null }
  | { type: 'hover'; id: number | null }
  | { type: 'highlight'; kind: HighlightKind; ids: readonly (string | number)[]; replace: boolean; focus: boolean }
  | { type: 'place'; place: PlaceMark; replace: boolean }
  | { type: 'clear' }
  | { type: 'smokeBackCity'; city: string };

function reducer(s: LiveState, a: Action): LiveState {
  switch (a.type) {
    case 'layers': {
      const next = a.only ? (Object.fromEntries(Object.keys(s.layers).map((k) => [k, false])) as Record<LiveLayer, boolean>) : { ...s.layers };
      for (const l of a.on ?? []) next[l] = true;
      for (const l of a.off ?? []) next[l] = false;
      return { ...s, layers: next };
    }
    case 'toggle':
      return { ...s, layers: { ...s.layers, [a.layer]: !s.layers[a.layer] } };
    case 'replay':
      return { ...s, replayDay: a.day, selectedScar: a.day === s.replayDay ? s.selectedScar : null };
    case 'select':
      return { ...s, selectedScar: a.id };
    case 'hover':
      return s.hoverScar === a.id ? s : { ...s, hoverScar: a.id };
    case 'highlight': {
      const ids = a.ids.map(String);
      const prev = a.replace ? [] : s.highlights[a.kind];
      return {
        ...s,
        highlights: { ...s.highlights, [a.kind]: [...new Set([...prev, ...ids])] },
        focusHighlights: a.focus ? s.focusHighlights + 1 : s.focusHighlights,
      };
    }
    case 'place':
      return { ...s, places: a.replace ? [a.place] : [...s.places, a.place] };
    case 'clear':
      return { ...s, highlights: EMPTY_HIGHLIGHTS, places: [], hoverScar: null };
    case 'smokeBackCity':
      return { ...s, smokeBackCity: a.city };
  }
}

export type LiveActions = {
  dispatch: (a: Action) => void;
  /** Move the camera now, or as soon as the map has loaded. */
  camera: (target: CameraTarget) => void;
  /** BaseMap's `mapRef`: registers the map and runs a camera move that was waiting for it. */
  registerMap: (map: MapRef | null) => void;
  mapRef: RefObject<MapRef | null>;
};

const LiveStateContext = createContext<LiveState | null>(null);
const LiveActionsContext = createContext<LiveActions | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const mapRef = useRef<MapRef | null>(null);
  const pending = useRef<CameraTarget | null>(null);

  const camera = useCallback((target: CameraTarget) => {
    if (mapRef.current) cameraFlyTo(mapRef.current, target);
    else pending.current = target;
  }, []);

  const registerMap = useCallback((map: MapRef | null) => {
    mapRef.current = map;
    if (map && pending.current) {
      const t = pending.current;
      pending.current = null;
      // the map exists before its style; wait one frame so the move is not swallowed by the first fit
      requestAnimationFrame(() => cameraFlyTo(map, t));
    }
  }, []);

  const actions = useMemo<LiveActions>(() => ({ dispatch, camera, registerMap, mapRef }), [camera, registerMap]);

  return (
    <LiveActionsContext value={actions}>
      <LiveStateContext value={state}>{children}</LiveStateContext>
    </LiveActionsContext>
  );
}

export function useLiveState(): LiveState {
  const s = use(LiveStateContext);
  if (!s) throw new Error('useLiveState must be used inside <LiveProvider>');
  return s;
}

export function useLiveActions(): LiveActions {
  const a = use(LiveActionsContext);
  if (!a) throw new Error('useLiveActions must be used inside <LiveProvider>');
  return a;
}
