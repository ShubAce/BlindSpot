'use client';

import type { Layer, PickingInfo } from '@deck.gl/core';
import { GeoJsonLayer, PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { MapRef } from 'react-map-gl/maplibre';

import {
  airLayers,
  BaseMap,
  describePick,
  firesLayer,
  MARK,
  rgba,
  riskLayers,
  satelliteLayers,
  scarsLayers,
  smokeLayers,
  TOOLTIP_STYLE,
  windLayer,
  type MapLayerInput,
  type TooltipContent,
} from '@/components/map';
import { mapClock } from '@/components/map/clock';
import { mapLabelFont } from '@/components/map/layers/common';
import { centroidOf, type AirLatestResponse, type AreaGeometry, type FiresResponse, type RiskResponse, type ScarsResponse, type SmokeBackResponse, type SmokeForwardResponse, type WindFieldResponse } from '@/lib/api';
import { dateIST, metricText, number, placeName } from '@/lib/format';
import type { PassWindow, Satellite } from '@/lib/orbits';

import { BACK_STYLE } from './legend';
import { useLiveActions, useLiveState } from './live-state';
import { useMapCommands } from './map-commands';
import { istDayEndUtc } from './data';
import type { LiveScarCase, LiveScarsResponse, SmokeBackLive } from './types';

/**
 * Everything MapLibre and deck.gl: loaded with next/dynamic (ssr: false) by MapPanel, so the rest of
 * the screen paints and reads its data before this chunk arrives. It draws what MapPanel fetched;
 * it fetches nothing itself.
 */

export type MapData = {
  fires?: FiresResponse;
  scars?: LiveScarsResponse;
  risk?: RiskResponse;
  smoke?: SmokeForwardResponse;
  smokeBack?: SmokeBackLive;
  wind?: WindFieldResponse;
  air?: AirLatestResponse;
  sats: readonly Satellite[];
  passes: readonly PassWindow[];
  now: Date | null;
  /** Scar case files for highlighted or selected scars that are not among the drawn ones. */
  cases: readonly LiveScarCase[];
};

const HL = rgba(MARK.ink, 255);
const HL_FILL = rgba(MARK.ink, 40);
const PLACE = rgba(MARK.ink, 230);

type HlPoint = { id: string; position: [number, number] };
type HlArea = { id: string; geometry: AreaGeometry };

function ringBounds(g: AreaGeometry, acc: [number, number, number, number]) {
  const rings = g.type === 'Polygon' ? g.coordinates : g.coordinates.flat();
  for (const ring of rings) for (const p of ring) {
    acc[0] = Math.min(acc[0], p[0]);
    acc[1] = Math.min(acc[1], p[1]);
    acc[2] = Math.max(acc[2], p[0]);
    acc[3] = Math.max(acc[3], p[1]);
  }
}

/** A layer (or an animated layer's every frame) drawn at `opacity`. */
function withOpacity(input: MapLayerInput, opacity: number): MapLayerInput {
  if (!input || Array.isArray(input)) return input;
  if ('animated' in input && input.animated) {
    const a = input;
    return { ...a, render: (f) => a.render(f)?.clone({ opacity }) ?? null };
  }
  return (input as Layer).clone({ opacity });
}

/** Risk scores arrive 0-100; the shared risk layer bins 0-1. Scale a copy for drawing only. */
const scaledRisk = new WeakMap<RiskResponse, RiskResponse>();
function riskForLayer(r: RiskResponse | undefined): RiskResponse | undefined {
  if (!r) return r;
  let out = scaledRisk.get(r);
  if (!out) {
    const max = Math.max(0, ...r.features.features.map((f) => f.properties.risk_score ?? 0));
    const k = max > 1 ? 100 : 1;
    out = { ...r, features: { ...r.features, features: r.features.features.map((f) => ({ ...f, properties: { ...f.properties, risk_score: (f.properties.risk_score ?? 0) / k } })) } };
    scaledRisk.set(r, out);
  }
  return out;
}

/**
 * Padding that keeps a framed field out from under a card floating over the map: the side the card
 * sits on gets the card's depth, the others a margin. Null when the card leaves too little map.
 */
function clearOf(container: HTMLElement, card: HTMLElement): { top: number; bottom: number; left: number; right: number } | null {
  const M = 48;
  const m = container.getBoundingClientRect();
  const c = card.getBoundingClientRect();
  const right = m.right - c.left + 16;
  if (c.left > m.left + m.width * 0.25 && m.width - right - M >= 120) return { top: M, bottom: M, left: M, right };
  const top = c.bottom - m.top + 16;
  if (m.height - top - M >= 100) return { top, bottom: M, left: M, right: M };
  return null;
}

export default function LiveMapCanvas({ data, label }: { data: MapData; label: string }) {
  const s = useLiveState();
  const { registerMap, camera, mapRef } = useLiveActions();
  const commands = useMapCommands();
  const on = s.layers;

  // in the replay, a detection's age is counted from the end of the day shown, not from now
  const fireClock = s.replayDay ? Date.parse(istDayEndUtc(s.replayDay)) : data.now ? data.now.getTime() : 0;
  const fireBucket = Math.floor(fireClock / 600_000);
  const fires = useMemo(() => (on.fires ? firesLayer(data.fires, { now: new Date(fireBucket * 600_000) }) : null), [on.fires, data.fires, fireBucket]);
  const scars = useMemo(() => (on.scars ? scarsLayers(data.scars as ScarsResponse | undefined, { selectedId: s.selectedScar }) : null), [on.scars, data.scars, s.selectedScar]);
  const risk = useMemo(() => (on.risk ? riskLayers(riskForLayer(data.risk)) : null), [on.risk, data.risk]);
  // 2,000 overlapping trails at the shared style's alpha bury the scars; drawn at half strength here
  const smoke = useMemo(() => (on.smoke ? smokeLayers(data.smoke).map((l) => withOpacity(l, 0.45)) : null), [on.smoke, data.smoke]);
  const wind = useMemo(() => (on.wind ? windLayer(data.wind) : null), [on.wind, data.wind]);
  const air = useMemo(() => (on.air ? airLayers(data.air) : null), [on.air, data.air]);
  const sats = useMemo(() => (on.satellites ? satelliteLayers(data.sats, data.now, { passes: data.passes }) : null), [on.satellites, data.sats, data.now, data.passes]);

  // smoke reaching a city: the two models' centre lines, their cone and the back trajectories
  const back = useMemo<MapLayerInput[] | null>(() => {
    const b = data.smokeBack;
    if (!on.smokeBack || !b) return null;
    const out: MapLayerInput[] = [];
    const cone = b.summary?.cone;
    if (cone) {
      out.push(
        new GeoJsonLayer({
          id: 'smoke:back-cone',
          data: [{ type: 'Feature', geometry: cone, properties: {} }] as never,
          filled: true,
          stroked: false,
          getFillColor: BACK_STYLE.cone,
          pickable: false,
        }),
      );
    }
    out.push(...smokeLayers(b as unknown as SmokeBackResponse, { id: 'smoke:back', showCone: false }));
    const lines = (b.summary?.members ?? []).filter((m) => (m.centerline?.path?.length ?? 0) > 1);
    if (lines.length) {
      out.push(
        new PathLayer<(typeof lines)[number]>({
          id: 'smoke:back-centre',
          data: lines,
          widthUnits: 'pixels',
          getPath: (m) => m.centerline?.path ?? [],
          getColor: (m) => (m.model === 'gfs_seamless' ? BACK_STYLE.line2 : BACK_STYLE.line),
          getWidth: 2,
          pickable: false,
        }),
      );
    }
    return out;
  }, [on.smokeBack, data.smokeBack]);

  // highlights: rings at the centre (visible at any zoom) and outlines of the shapes
  const hl = useMemo(() => {
    const points: HlPoint[] = [];
    const areas: HlArea[] = [];
    const ids = new Set([...s.highlights.scar, ...(s.hoverScar != null ? [String(s.hoverScar)] : [])]);
    if (ids.size) {
      const seen = new Set<string>();
      for (const f of data.scars?.features.features ?? []) {
        const id = String(f.properties.id);
        if (!ids.has(id) || seen.has(id)) continue;
        seen.add(id);
        areas.push({ id, geometry: f.geometry });
        const c = centroidOf(f.properties.centroid);
        if (c) points.push({ id, position: c });
      }
      for (const c of data.cases) {
        const id = String(c.case.id);
        if (!ids.has(id) || seen.has(id)) continue;
        seen.add(id);
        if (c.case.geometry) areas.push({ id, geometry: c.case.geometry });
        const p = centroidOf(c.case.centroid);
        if (p) points.push({ id, position: p });
      }
    }
    if (s.highlights.risk_cluster.length) {
      const want = new Set(s.highlights.risk_cluster);
      for (const f of data.risk?.features.features ?? []) {
        if (!want.has(String(f.properties.id))) continue;
        areas.push({ id: `r${f.properties.id}`, geometry: f.geometry });
        const c = centroidOf(f.properties.centroid);
        if (c) points.push({ id: `r${f.properties.id}`, position: c });
      }
    }
    if (s.highlights.fire.length) {
      const want = new Set(s.highlights.fire);
      for (const f of data.fires?.features.features ?? []) if (want.has(String(f.properties.id))) points.push({ id: `f${f.properties.id}`, position: [f.geometry.coordinates[0], f.geometry.coordinates[1]] });
    }
    if (s.highlights.station.length) {
      const want = new Set(s.highlights.station);
      for (const st of data.air?.stations ?? []) if (want.has(String(st.id))) points.push({ id: `s${st.id}`, position: [st.lon, st.lat] });
    }
    return { points, areas };
  }, [s.highlights, s.hoverScar, data.scars, data.cases, data.risk, data.fires, data.air]);

  const hlLayers = useMemo<Layer[]>(() => {
    const out: Layer[] = [];
    if (hl.areas.length) {
      out.push(
        new GeoJsonLayer({
          id: 'highlight:areas',
          data: hl.areas.map((a) => ({ type: 'Feature', geometry: a.geometry, properties: { id: a.id } })) as never,
          filled: true,
          stroked: true,
          lineWidthUnits: 'pixels',
          getFillColor: HL_FILL,
          getLineColor: HL,
          getLineWidth: 2,
          pickable: false,
        }),
      );
    }
    const rings = [...hl.points.map((p) => ({ position: p.position, label: null as string | null })), ...s.places.map((p) => ({ position: [p.lon, p.lat] as [number, number], label: p.label ?? null }))];
    if (rings.length) {
      out.push(
        new ScatterplotLayer<(typeof rings)[number]>({
          id: 'highlight:rings',
          data: rings,
          radiusUnits: 'pixels',
          lineWidthUnits: 'pixels',
          filled: false,
          stroked: true,
          getPosition: (d) => d.position,
          getRadius: 12,
          getLineColor: PLACE,
          getLineWidth: 1.5,
          pickable: false,
        }),
      );
      const labelled = rings.filter((r) => r.label);
      if (labelled.length) {
        out.push(
          new TextLayer<(typeof labelled)[number]>({
            id: 'highlight:labels',
            data: labelled,
            getPosition: (d) => d.position,
            getText: (d) => d.label ?? '',
            getPixelOffset: [16, 0],
            getSize: 12,
            sizeUnits: 'pixels',
            getColor: PLACE,
            getTextAnchor: 'start',
            getAlignmentBaseline: 'center',
            fontFamily: mapLabelFont(),
            fontWeight: 500,
            fontSettings: { sdf: true },
            outlineWidth: 3,
            outlineColor: rgba(MARK.ground, 255),
          }),
        );
      }
    }
    return out;
  }, [hl, s.places]);

  // frame the highlighted features once their geometry is here (a click in the list, an agent's highlight)
  const framed = useRef(0);
  useEffect(() => {
    if (s.focusHighlights === framed.current) return;
    const acc: [number, number, number, number] = [Infinity, Infinity, -Infinity, -Infinity];
    for (const a of hl.areas) ringBounds(a.geometry, acc);
    for (const p of hl.points) {
      acc[0] = Math.min(acc[0], p.position[0]);
      acc[1] = Math.min(acc[1], p.position[1]);
      acc[2] = Math.max(acc[2], p.position[0]);
      acc[3] = Math.max(acc[3], p.position[1]);
    }
    if (!Number.isFinite(acc[0])) return; // geometry not here yet; try again when it lands
    framed.current = s.focusHighlights;
    const pad = 0.01; // about a kilometre around one field, so its neighbours show
    const bounds: [number, number, number, number] = [acc[0] - pad, acc[1] - pad, acc[2] + pad, acc[3] + pad];
    // with a case summary open, frame the field in the part of the map the card leaves clear
    const map = mapRef.current?.getMap();
    const container = map?.getContainer();
    const card = container?.closest('.lv-mapBody')?.querySelector<HTMLElement>('.lv-caseCard');
    const clear = container && card ? clearOf(container, card) : null;
    if (map && clear) map.fitBounds(bounds, { padding: clear, duration: mapClock.isReduced ? 0 : 1200, maxZoom: 13 });
    else camera({ bounds, padding: 64 });
  }, [s.focusHighlights, hl, camera, mapRef]);

  const layers = useMemo<MapLayerInput[]>(
    () => [risk, scars, smoke, back, wind, air, fires, sats, hlLayers],
    [risk, scars, smoke, back, wind, air, fires, sats, hlLayers],
  );

  const getTooltip = useCallback((info: PickingInfo): TooltipContent => {
    const id = info.layer?.id ?? '';
    if (id.startsWith('risk') && info.object) {
      const o = info.object as { f?: { properties: Record<string, unknown> }; properties?: Record<string, unknown> };
      const p = (o.f ?? o).properties as { id: number; area_ha: number; days_since_harvest: number | null; harvested_after?: string; harvested_before?: string; village?: string | null; district?: string | null } | undefined;
      if (!p) return null;
      const original = data.risk?.features.features.find((f) => f.properties.id === p.id)?.properties.risk_score;
      const text = [
        `Harvested, not yet burnt: risk ${number(original, { decimals: 1 })} of 100`,
        `${metricText('area_ha', p.area_ha)}${p.days_since_harvest != null ? `, ${number(p.days_since_harvest)} days since harvest` : ''}`,
        p.harvested_after && p.harvested_before ? `Cut between ${dateIST(p.harvested_after)} and ${dateIST(p.harvested_before)}` : null,
        placeName(p.village ? `near ${p.village}` : null, p.district),
      ]
        .filter(Boolean)
        .join('\n');
      return { text, className: 'map-tooltip', style: TOOLTIP_STYLE };
    }
    const text = describePick(info);
    return text ? { text, className: 'map-tooltip', style: TOOLTIP_STYLE } : null;
  }, [data.risk]);

  const onPick = useCallback(
    (info: PickingInfo) => {
      if (!info.layer?.id.startsWith('scars') || !info.object) return;
      const o = info.object as { f?: { properties: { id: number } }; properties?: { id: number } };
      const id = (o.f ?? o).properties?.id;
      if (id != null) commands.openCase(id);
    },
    [commands],
  );

  const setRef = useCallback((m: MapRef | null) => registerMap(m), [registerMap]);
  useEffect(() => () => registerMap(null), [registerMap]);

  // MapLibre opens the credits on a wide map, and again each time the credits change (BaseMap
  // remounts the control); keep them behind their (i) button so they do not cover the key and the
  // replay. They stay one click away.
  const onLoad = useCallback(() => {
    const container = mapRef.current?.getMap().getContainer();
    if (!container) return;
    const fold = (el: Element | null | undefined) => el?.classList.remove('maplibregl-compact-show');
    fold(container.querySelector('.maplibregl-ctrl-attrib'));
    const corner = container.querySelector('.maplibregl-ctrl-bottom-right');
    if (!corner) return;
    const mo = new MutationObserver((records) => {
      for (const r of records) for (const n of r.addedNodes) if (n instanceof Element && n.classList.contains('maplibregl-ctrl-attrib')) requestAnimationFrame(() => fold(n));
    });
    mo.observe(corner, { childList: true });
    observer.current?.disconnect();
    observer.current = mo;
  }, [mapRef]);
  const observer = useRef<MutationObserver | null>(null);
  useEffect(() => () => observer.current?.disconnect(), []);

  return <BaseMap layers={layers} getTooltip={getTooltip} onPick={onPick} onLoad={onLoad} mapRef={setRef} label={label} />;
}
