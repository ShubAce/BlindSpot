'use client';

import type { Layer, PickingInfo } from '@deck.gl/core';
import { GeoJsonLayer, PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import type { SymbolLayerSpecification } from 'maplibre-gl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Layer as MapLayer, Source, type MapRef } from 'react-map-gl/maplibre';

import {
  airLayers,
  BaseMap,
  binScars,
  describeDistrictTile,
  describeScar,
  describeScarTile,
  districtNameAt,
  districtTiles,
  describePick,
  firesLayer,
  mercatorMetresPerPixel,
  paletteFor,
  riskLayers,
  satelliteLayers,
  SCAR_TILE_GEOMETRY,
  scarsLayers,
  scarTilesLayer,
  smokeLayers,
  TOOLTIP_STYLE,
  windLayer,
  type DistrictShape,
  type MapLayerInput,
  type MapPalette,
  type ScarFeature,
  type ScarTile,
  type TooltipContent,
} from '@/components/map';
import { mapClock } from '@/components/map/clock';
import { mapLabelFont } from '@/components/map/layers/common';
import { nextLookParts, placeLookLabel } from '@/components/map/look';
import { centroidOf, readBoundaries, type AirLatestResponse, type AreaGeometry, type FiresResponse, type RiskResponse, type ScarsResponse, type SmokeBackResponse, type SmokeForwardResponse, type WindFieldResponse } from '@/lib/api';
import { dateIST, metricText, number, placeName } from '@/lib/format';
import { useBoundaries } from '@/lib/live';
import type { LatLon, PassWindow, Satellite } from '@/lib/orbits';
import { useTheme, type Theme } from '@/lib/theme';

import { useLiveActions, useLiveState } from './live-state';
import { useMapCommands } from './map-commands';
import { istDayEndUtc, precisionText, useLiveHeadline, useScarsSummaryLive } from './data';
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
  /** The site the next look is computed for (the headline's Punjab point). */
  site?: LatLon | null;
  now: Date | null;
  /** Scar case files for highlighted or selected scars that are not among the drawn ones. */
  cases: readonly LiveScarCase[];
  /** The replay's fires and scars, when `fires` and `scars` above are still the last day's while this
   *  day loads (MapPanel). Neither may read as this day: the scars stay, faded, so the map keeps its
   *  ground between days, and the fires step aside until their own day lands (a deck.gl scatter layer
   *  drew the same at any opacity, measured on screen, so fading them was no cue). */
  held?: { fires: boolean; scars: boolean };
};

/** How strongly the last replay day's scars show while the next day loads. */
const HELD_SCARS_OPACITY = 0.35;

const css = (c: readonly number[]) => `rgba(${c[0]},${c[1]},${c[2]},${(c[3] ?? 255) / 255})`;
const LOOK_LABEL_LAYOUT: SymbolLayerSpecification['layout'] = {
  'text-field': ['get', 'label'],
  'text-rotate': ['get', 'rotate'],
  'text-font': ['Noto Sans Regular'],
  'text-size': 12,
  'text-rotation-alignment': 'map',
  'text-anchor': 'bottom',
  'text-offset': [0, -0.25],
  'text-allow-overlap': true,
};
/** The next look's label, written by the basemap in the satellites' ink on a ground halo. A new paint
 *  object per theme is enough: react-map-gl diffs it into setPaintProperty. */
const lookLabelPaint = ({ SATELLITE_STYLE, MARK }: MapPalette): SymbolLayerSpecification['paint'] => ({
  'text-color': css(SATELLITE_STYLE.label),
  'text-halo-color': MARK.ground,
  'text-halo-width': 2,
});
const LOOK_LABEL_PAINT: Readonly<Record<Theme, SymbolLayerSpecification['paint']>> = { dark: lookLabelPaint(paletteFor('dark')), light: lookLabelPaint(paletteFor('light')) };

const TIP_W = 280; // the tooltip's max-width (TOOLTIP_STYLE)
const TIP_GAP = 12; // from the pointer
const TIP_EDGE = 8; // kept clear of the map's frame

/** Tooltips open beside the pointer and always inside the map's frame: right of the pointer where
 *  they fit, else left of it, else (a phone's narrow map) as far left as the frame allows; above the
 *  pointer near the lower edge. deck.gl sets the tooltip's `transform` to the pointer; the separate
 *  CSS `translate` shifts it from there. */
function edgeAware(info: PickingInfo, t: TooltipContent): TooltipContent {
  if (!t || typeof t === 'string') return t;
  const w = info.viewport?.width ?? 0;
  const h = info.viewport?.height ?? 0;
  const tw = w > 0 ? Math.min(TIP_W, w - 2 * TIP_EDGE) : TIP_W;
  let tx = `${TIP_GAP}px`;
  if (w > 0 && info.x + TIP_GAP + tw > w - TIP_EDGE) {
    tx = info.x - TIP_GAP - tw >= TIP_EDGE
      ? `calc(-100% - ${TIP_GAP}px)`
      : `${Math.round(Math.max(TIP_EDGE, Math.min(info.x - tw / 2, w - TIP_EDGE - tw)) - info.x)}px`;
  }
  const up = h > 0 && info.y > h - 175;
  return {
    ...t,
    style: { ...t.style, maxWidth: `${tw}px`, translate: `${tx} ${up ? `calc(-100% - ${TIP_GAP}px)` : `${TIP_GAP}px`}` } as Partial<CSSStyleDeclaration>,
  };
}

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

/** A layer (or an animated layer's every frame, or each layer of a list) drawn at `opacity` times
 *  its own. */
function withOpacity(input: MapLayerInput, opacity: number): MapLayerInput {
  if (!input) return input;
  if (Array.isArray(input)) return (input as readonly MapLayerInput[]).map((l) => withOpacity(l, opacity));
  const times = (l: Layer) => l.clone({ opacity: (l.props.opacity ?? 1) * opacity });
  if ('animated' in input && input.animated) {
    const a = input;
    return { ...a, render: (f) => { const l = a.render(f); return l ? times(l) : null; } };
  }
  return times(input as Layer);
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
  const { registerMap, camera, mapRef, dispatch } = useLiveActions();
  const commands = useMapCommands();
  const on = s.layers;
  // The map paints its own colours (MapLibre's style, deck.gl's layers), so it follows the theme in
  // code: every layer below is rebuilt with the theme's marks when it changes. This chunk loads after
  // hydration, so the first map already has the right theme.
  const theme = useTheme();

  // the zoom and view, read when a move ends: zoomed out (live) the season by district, then the
  // drawn fields binned into tiles below SCAR_TILE_GEOMETRY.fieldsZoom, then the fields
  const [zoom, setZoom] = useState<number | null>(null);
  const [view, setView] = useState<[number, number, number, number] | null>(null);
  const binZoom = zoom == null ? null : Math.round(zoom * 2) / 2;
  const summary = useScarsSummaryLive();
  const districtShapes = useBoundaries({ level: 'district' });
  const shapes = useMemo<DistrictShape[]>(() => {
    const spec = districtShapes.data ? readBoundaries(districtShapes.data) : null;
    return spec?.kind === 'geojson' ? (spec.data.features as unknown as DistrictShape[]) : [];
  }, [districtShapes.data]);
  const wantDistricts = !s.replayDay && (zoom == null || zoom < SCAR_TILE_GEOMETRY.districtZoom);
  const districts = useMemo(
    () => (on.scars && wantDistricts && binZoom != null ? districtTiles(summary.data?.by_district ?? [], shapes, binZoom) : []),
    [on.scars, wantDistricts, binZoom, summary.data, shapes],
  );
  const scarsView = districts.length ? 'districts' : zoom == null || zoom < SCAR_TILE_GEOMETRY.fieldsZoom ? 'tiles' : 'fields';
  useEffect(() => {
    dispatch({ type: 'scarsView', view: scarsView });
  }, [scarsView, dispatch]);
  // Zoomed in past the district tiles, however the map got there (the wheel, a location link, the
  // investigator), it also loads the district under its centre, as a click on that district's tile
  // does: the newest scars overall miss whole districts (Moga, Amritsar), which would otherwise
  // look unburnt up close. Live only, like the district read itself.
  const rows = summary.data?.by_district;
  useEffect(() => {
    if (s.replayDay || zoom == null || zoom < SCAR_TILE_GEOMETRY.districtZoom || !view || !shapes.length || !rows?.length) return;
    const name = districtNameAt(rows, shapes, [(view[0] + view[2]) / 2, (view[1] + view[3]) / 2]);
    if (name) dispatch({ type: 'scarDistrict', district: name });
  }, [s.replayDay, zoom, view, shapes, rows, dispatch]);

  // in the replay, a detection's age is counted from the end of the day shown, not from now
  const fireClock = s.replayDay ? Date.parse(istDayEndUtc(s.replayDay)) : data.now ? data.now.getTime() : 0;
  const fireBucket = Math.floor(fireClock / 600_000);
  const heldFires = !!data.held?.fires;
  const heldScars = !!data.held?.scars;
  const fires = useMemo(
    () => (on.fires && !heldFires ? firesLayer(data.fires, { now: new Date(fireBucket * 600_000), theme }) : null),
    [on.fires, data.fires, fireBucket, theme, heldFires],
  );
  // tiles are re-binned per half zoom step, so a pinch does not rebuild them on every frame
  const scars = useMemo(() => {
    if (!on.scars) return null;
    if (scarsView === 'districts') return scarTilesLayer(districts, { theme });
    let layers: MapLayerInput;
    if (scarsView === 'tiles') {
      if (binZoom == null) return null;
      layers = scarTilesLayer(binScars((data.scars?.features.features ?? []) as unknown as ScarFeature[], binZoom), { theme });
    } else layers = scarsLayers(data.scars as ScarsResponse | undefined, { selectedId: s.selectedScar, theme });
    return heldScars ? withOpacity(layers, HELD_SCARS_OPACITY) : layers;
  }, [on.scars, scarsView, districts, binZoom, data.scars, s.selectedScar, theme, heldScars]);
  const risk = useMemo(() => (on.risk ? riskLayers(riskForLayer(data.risk), { theme }) : null), [on.risk, data.risk, theme]);
  // 2,000 overlapping trails at the shared style's alpha bury the scars; drawn at half strength here
  const smoke = useMemo(() => (on.smoke ? smokeLayers(data.smoke, { theme }).map((l) => withOpacity(l, 0.45)) : null), [on.smoke, data.smoke, theme]);
  const wind = useMemo(() => (on.wind ? windLayer(data.wind, { theme }) : null), [on.wind, data.wind, theme]);
  const air = useMemo(() => (on.air ? airLayers(data.air, { theme }) : null), [on.air, data.air, theme]);
  // the next look at Punjab: the line its scanner sweeps through the site and that pass's track
  // (its whole-swath footprint would only tint the entire view: VIIRS sees 3,040 km at once)
  // Its label is a basemap symbol when the basemap has fonts, placed first, so a town name under it
  // gives way instead of being covered; it sits off the burn belt's marks where the view allows.
  const [glyphs, setGlyphs] = useState(false);
  const nextLook = useMemo(() => (data.site && data.passes[0] ? { pass: data.passes[0], site: data.site, label: !glyphs } : null), [data.site, data.passes, glyphs]);
  const sats = useMemo(() => (on.satellites ? satelliteLayers(data.sats, data.now, { nextLook, theme }) : null), [on.satellites, data.sats, data.now, nextLook, theme]);
  const look = useMemo(() => (on.satellites && glyphs && nextLook ? nextLookParts(data.sats, nextLook.pass, nextLook.site) : null), [on.satellites, glyphs, nextLook, data.sats]);
  const marks = useMemo(() => {
    const out: [number, number][] = [];
    if (on.scars) {
      for (const f of data.scars?.features.features ?? []) {
        const c = centroidOf(f.properties.centroid);
        if (c) out.push(c);
      }
    }
    if (on.fires) for (const f of data.fires?.features.features ?? []) out.push([f.geometry.coordinates[0], f.geometry.coordinates[1]]);
    return out;
  }, [on.scars, on.fires, data.scars, data.fires]);
  const lookLabel = useMemo(() => {
    if (!look) return null;
    // ground km per screen pixel at the view's centre latitude
    const kmPerPx = zoom != null && view ? (mercatorMetresPerPixel(zoom) * Math.cos((((view[1] + view[3]) / 2) * Math.PI) / 180)) / 1000 : 0;
    const p = placeLookLabel(look, view, marks, kmPerPx);
    return { type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: p.at }, properties: { label: look.label, rotate: -p.angle } };
  }, [look, view, marks, zoom]);

  // smoke reaching a city: the two models' centre lines, their cone and the back trajectories
  const back = useMemo<MapLayerInput[] | null>(() => {
    const b = data.smokeBack;
    if (!on.smokeBack || !b) return null;
    const { BACK_STYLE } = paletteFor(theme);
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
    out.push(...smokeLayers(b as unknown as SmokeBackResponse, { id: 'smoke:back', showCone: false, theme }));
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
          // deck.gl never compares accessor functions: the theme is what tells it the colours changed
          updateTriggers: { getColor: [theme] },
        }),
      );
    }
    return out;
  }, [on.smokeBack, data.smokeBack, theme]);

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
    const { FOCUS_STYLE: F } = paletteFor(theme);
    const out: Layer[] = [];
    if (hl.areas.length) {
      out.push(
        new GeoJsonLayer({
          id: 'highlight:areas',
          data: hl.areas.map((a) => ({ type: 'Feature', geometry: a.geometry, properties: { id: a.id } })) as never,
          filled: true,
          stroked: true,
          lineWidthUnits: 'pixels',
          getFillColor: F.fill,
          getLineColor: F.line,
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
          getLineColor: F.label,
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
            getColor: F.label,
            getTextAnchor: 'start',
            getAlignmentBaseline: 'center',
            fontFamily: mapLabelFont(),
            fontWeight: 500,
            fontSettings: { sdf: true },
            outlineWidth: 3,
            outlineColor: F.halo,
          }),
        );
      }
    }
    return out;
  }, [hl, s.places, theme]);

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

  // every candidate unseen burn on the map carries the audit's precision and its interval
  const head = useLiveHeadline();
  const precision = precisionText(head.data?.unseen_burns);
  const precisionLine = precision ? `Candidate unseen burns: ${precision}` : null;
  const since = summary.data?.season?.since;
  const summarySince = since ? dateIST(`${since}T12:00:00+05:30`) : null;

  const getTooltip = useCallback((info: PickingInfo): TooltipContent => {
    const id = info.layer?.id ?? '';
    if (id.startsWith('scars') && info.object) {
      const o = info.object as Record<string, unknown>;
      let text: string;
      if (id === 'scars:tiles' && (o as unknown as ScarTile).district) text = describeDistrictTile(o as unknown as ScarTile, summarySince, precisionLine);
      else if (id === 'scars:tiles') text = describeScarTile(o as unknown as ScarTile, precisionLine);
      else {
        const f = ('f' in o ? o.f : o) as ScarFeature;
        text = describeScar(f);
        if (precisionLine && f.properties?.unseen === true) text = `${text}\n${precisionLine}`;
      }
      return edgeAware(info, { text, className: 'map-tooltip', style: TOOLTIP_STYLE });
    }
    if (id.startsWith('risk') && info.object) {
      const o = info.object as { f?: { properties: Record<string, unknown> }; properties?: Record<string, unknown> };
      const p = (o.f ?? o).properties as { id: number; area_ha: number; days_since_harvest: number | null; harvested_after?: string; harvested_before?: string; village?: string | null; district?: string | null } | undefined;
      if (!p) return null;
      const full = data.risk?.features.features.find((f) => f.properties.id === p.id)?.properties;
      const original = full?.risk_score;
      // A relative ranking, not a probability: say where the field ranks when the API gives it.
      const head =
        full?.risk_rank != null && full.risk_rank_of != null
          ? `Harvested, not yet burnt: ranked ${number(full.risk_rank)} of ${number(full.risk_rank_of)} by burn risk${full.risk_band ? ` (${full.risk_band})` : ''}`
          : `Harvested, not yet burnt: risk index ${number(original, { decimals: 1 })} (a ranking, not a probability)`;
      const text = [
        head,
        `${metricText('area_ha', p.area_ha)}${p.days_since_harvest != null ? `, ${number(p.days_since_harvest)} days since harvest` : ''}`,
        p.harvested_after && p.harvested_before ? `Cut between ${dateIST(p.harvested_after)} and ${dateIST(p.harvested_before)}` : null,
        placeName(p.village ? `near ${p.village}` : null, p.district),
      ]
        .filter(Boolean)
        .join('\n');
      return edgeAware(info, { text, className: 'map-tooltip', style: TOOLTIP_STYLE });
    }
    const text = describePick(info);
    return text ? edgeAware(info, { text, className: 'map-tooltip', style: TOOLTIP_STYLE }) : null;
  }, [data.risk, precisionLine, summarySince]);

  const onPick = useCallback(
    (info: PickingInfo) => {
      if (!info.layer?.id.startsWith('scars') || !info.object) return;
      // A tap is followed about 100 ms later by the browser's compatibility mousedown, and MapLibre
      // stops any flight a mousedown meets: start the camera after it (unnoticeable with a mouse).
      const later = (move: () => void) => window.setTimeout(move, 150);
      if (info.layer.id === 'scars:tiles') {
        const t = info.object as ScarTile;
        // a district opens onto its own newest scars (loaded for it) at district scale
        if (t.district) {
          dispatch({ type: 'scarDistrict', district: t.district.name });
          later(() => camera({ bounds: t.extent, padding: 32 }));
          return;
        }
        // a tile opens onto its fields: frame them, with at least ~3 km around so a lone field keeps
        // its neighbourhood, and never short of the zoom where the fields are drawn
        const [w, so, e, n] = t.extent;
        const pad = 0.015;
        const cx = (w + e) / 2;
        const cy = (so + n) / 2;
        const hx = Math.max((e - w) / 2, pad);
        const hy = Math.max((n - so) / 2, pad);
        const bounds: [number, number, number, number] = [cx - hx, cy - hy, cx + hx, cy + hy];
        later(() => {
          const map = mapRef.current?.getMap();
          const cam = map?.cameraForBounds(bounds, { padding: 48 });
          if (map && cam?.center != null && cam.zoom != null) {
            const zoomTo = Math.min(13, Math.max(cam.zoom, SCAR_TILE_GEOMETRY.fieldsZoom + 0.1));
            map.flyTo({ center: cam.center, zoom: zoomTo, duration: mapClock.isReduced ? 0 : 1200 });
          } else camera({ bounds, padding: 48 });
        });
        return;
      }
      const o = info.object as { f?: { properties: { id: number } }; properties?: { id: number } };
      const id = (o.f ?? o).properties?.id;
      if (id != null) commands.openCase(id);
    },
    [commands, camera, dispatch, mapRef],
  );

  const setRef = useCallback((m: MapRef | null) => registerMap(m), [registerMap]);
  useEffect(() => () => registerMap(null), [registerMap]);

  // MapLibre opens the credits on a wide map, and again each time the credits change (BaseMap
  // remounts the control); keep them behind their (i) button so they do not cover the key and the
  // replay. They stay one click away.
  const onLoad = useCallback(() => {
    const map = mapRef.current?.getMap();
    if (map) {
      const read = () => {
        setZoom(map.getZoom());
        const b = map.getBounds();
        setView([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
      };
      read();
      setGlyphs(!!map.getStyle()?.glyphs);
      zoomOff.current?.();
      map.on('moveend', read);
      zoomOff.current = () => map.off('moveend', read);
    }
    const container = map?.getContainer();
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
  const zoomOff = useRef<(() => void) | null>(null);
  useEffect(
    () => () => {
      observer.current?.disconnect();
      zoomOff.current?.();
    },
    [],
  );

  return (
    <BaseMap layers={layers} theme={theme} getTooltip={getTooltip} onPick={onPick} onLoad={onLoad} mapRef={setRef} label={label} foreignLabels="dim">
      {lookLabel ? (
        <Source id="lv-look-label" type="geojson" data={lookLabel}>
          <MapLayer id="lv-look-label" type="symbol" layout={LOOK_LABEL_LAYOUT} paint={LOOK_LABEL_PAINT[theme]} />
        </Source>
      ) : null}
    </BaseMap>
  );
}
