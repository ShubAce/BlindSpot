'use client';

import { useMemo } from 'react';

import { useLiveActions, type HighlightKind, type LiveLayer, type PlaceMark } from './live-state';

/**
 * The map's command API, for the ranked list, the replay and the investigator console.
 * The console calls these; it never reaches into the map itself. See docs/build-logs/live.md,
 * "Investigator contract".
 *
 *   const map = useMapCommands();
 *   map.flyTo({ lon: 75.84, lat: 30.25, zoom: 10 });
 *   map.flyTo({ bbox: [75.2, 29.9, 76.1, 30.6] });
 *   map.highlight('scar', [42623], { focus: true });
 *   map.showLayers(['smokeBack'], { city: 'Delhi' });
 *   map.applyAgentEvent(ev);            // an SSE `map` event, as the agent sent it
 *   map.clearHighlights();
 */

export type FlyTarget = { lon: number; lat: number; zoom?: number } | { bbox: readonly [number, number, number, number]; padding?: number };

export type MapCommands = {
  /** Glide to a point (zoom 4-15, default: keep the zoom, at least 10) or frame a box. Jumps under reduced motion. */
  flyTo: (target: FlyTarget) => void;
  /** Outline features of one kind by id. `replace` (default true) drops earlier ids of that kind;
   *  `focus` frames them once their geometry is known (scars not on the map are fetched). */
  highlight: (kind: HighlightKind, ids: readonly (string | number)[], opts?: { replace?: boolean; focus?: boolean }) => void;
  /** Mark a place (district, city, village or point) with a ring and its label. */
  markPlace: (place: PlaceMark, opts?: { replace?: boolean }) => void;
  /** Switch layers on. `only` switches every other layer off. `city` picks the back-trajectory city. */
  showLayers: (layers: readonly LiveLayer[], opts?: { only?: boolean; city?: string }) => void;
  hideLayers: (layers: readonly LiveLayer[]) => void;
  /** Remove every highlight and place mark. */
  clearHighlights: () => void;
  /** Open (or with null, close) a scar's case summary and fly to it. */
  openCase: (id: number | null) => void;
  /** Leave the season replay and show live data again. */
  goLive: () => void;
  /** Apply an agent `map` event ({action, payload}) as the API sends it. Returns false if it was not understood. */
  applyAgentEvent: (ev: { action: string; payload: Record<string, unknown> }) => boolean;
};

/** The agent's layer names (services/api agent/tools.py MAP_LAYERS) on this screen's layers. */
export const AGENT_LAYERS: Readonly<Record<string, LiveLayer | null>> = {
  fires: 'fires',
  scars: 'scars',
  unseen_scars: 'scars',
  risk: 'risk',
  smoke_forward: 'smoke',
  smoke_back: 'smokeBack',
  air_stations: 'air',
  wind: 'wind',
  satellite_passes: 'satellites',
  imagery: null,
};

/** The agent's highlight types on this screen's kinds; districts, cities and villages are places. */
const AGENT_KINDS: Readonly<Record<string, HighlightKind>> = { scar: 'scar', risk_cluster: 'risk_cluster', fire: 'fire', station: 'station' };
const KIND_LAYER: Readonly<Record<HighlightKind, LiveLayer>> = { scar: 'scars', risk_cluster: 'risk', fire: 'fires', station: 'air' };

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
const pair = (v: unknown): [number, number] | null => {
  if (!Array.isArray(v) || v.length !== 2) return null;
  const a = num(v[0]);
  const b = num(v[1]);
  return a != null && b != null ? [a, b] : null;
};
const box = (v: unknown): [number, number, number, number] | null => {
  if (!Array.isArray(v) || v.length !== 4) return null;
  const n = v.map(num);
  return n.every((x): x is number => x != null) ? (n as [number, number, number, number]) : null;
};

export function useMapCommands(): MapCommands {
  const { dispatch, camera } = useLiveActions();
  return useMemo<MapCommands>(() => {
    const flyTo: MapCommands['flyTo'] = (t) => {
      if ('bbox' in t) camera({ bounds: [t.bbox[0], t.bbox[1], t.bbox[2], t.bbox[3]], padding: t.padding });
      else camera({ lon: t.lon, lat: t.lat, zoom: t.zoom });
    };
    const highlight: MapCommands['highlight'] = (kind, ids, opts = {}) => {
      dispatch({ type: 'layers', on: [KIND_LAYER[kind]] });
      dispatch({ type: 'highlight', kind, ids, replace: opts.replace ?? true, focus: opts.focus ?? false });
    };
    const markPlace: MapCommands['markPlace'] = (place, opts = {}) => dispatch({ type: 'place', place, replace: opts.replace ?? true });
    const showLayers: MapCommands['showLayers'] = (layers, opts = {}) => {
      if (opts.city) dispatch({ type: 'smokeBackCity', city: opts.city });
      dispatch({ type: 'layers', on: layers, only: opts.only });
    };
    const hideLayers: MapCommands['hideLayers'] = (layers) => dispatch({ type: 'layers', off: layers });
    const clearHighlights = () => dispatch({ type: 'clear' });
    const openCase: MapCommands['openCase'] = (id) => {
      dispatch({ type: 'select', id });
      if (id != null) {
        dispatch({ type: 'layers', on: ['scars'] });
        dispatch({ type: 'highlight', kind: 'scar', ids: [id], replace: true, focus: true });
      }
    };
    const goLive = () => dispatch({ type: 'replay', day: null });

    const applyAgentEvent: MapCommands['applyAgentEvent'] = ({ action, payload }) => {
      const p = payload ?? {};
      const label = typeof p.label === 'string' ? p.label : null;
      if (action === 'show_layer') {
        const layer = typeof p.layer === 'string' ? AGENT_LAYERS[p.layer] : undefined;
        if (!layer) return false;
        const params = (p.params ?? {}) as Record<string, unknown>;
        if (p.visible === false) hideLayers([layer]);
        else showLayers([layer], { city: typeof params.city === 'string' ? params.city : undefined });
        return true;
      }
      const center = pair(p.center);
      const bbox = box(p.bbox);
      if (action === 'fly_to') {
        if (bbox) flyTo({ bbox });
        else if (center) flyTo({ lon: center[0], lat: center[1], zoom: num(p.zoom) ?? undefined });
        else return false;
        if (center) markPlace({ lon: center[0], lat: center[1], label });
        return true;
      }
      if (action === 'highlight') {
        const kind = typeof p.feature_type === 'string' ? AGENT_KINDS[p.feature_type] : undefined;
        const id = p.id;
        if (kind && (typeof id === 'string' || typeof id === 'number')) {
          if (kind === 'scar') openCase(num(id));
          else highlight(kind, [id], { focus: !center && !bbox });
        }
        if (bbox) flyTo({ bbox });
        else if (center) flyTo({ lon: center[0], lat: center[1], zoom: num(p.zoom) ?? undefined });
        if (center && !(kind && id != null)) markPlace({ lon: center[0], lat: center[1], label });
        return !!(kind || center || bbox);
      }
      return false;
    };

    return { flyTo, highlight, markPlace, showLayers, hideLayers, clearHighlights, openCase, goLive, applyAgentEvent };
  }, [camera, dispatch]);
}
