/**
 * The basemap, made India-compliant and warm (docs/research/geography-and-basemaps.md sections 8-9).
 *
 * OpenFreeMap "dark" (keyless, no request limits, OpenMapTiles schema) with:
 * - every boundary layer removed: the national and state lines on our maps come only from
 *   /api/geo/boundaries (Survey of India), never from a basemap's own depiction;
 * - the "Azad Kashmir", "Gilgit-Baltistan" and "Aksai Chin" region labels filtered out;
 * - its neutral greys turned toward the warm black of the page (#0c0a08 ground);
 * - two invisible anchor layers before the first place label, so deck.gl data and our boundary lines
 *   sit above land and roads but under the labels;
 * - an optional EOX Sentinel-2 cloudless 2025 raster (keyless, CC BY-NC-SA 4.0) for the imagery view.
 * If OpenFreeMap cannot be reached, `fallbackStyle` keeps the map working on a plain ground: our
 * boundaries and data still draw, and the page says the basemap is missing.
 */

import type { LayerSpecification, StyleSpecification } from 'maplibre-gl';

import { MARK } from './palette';
import { BOUNDARY_ANCHOR, DATA_ANCHOR, TOP_ANCHOR } from './types';

export const OFM_DARK_STYLE_URL = 'https://tiles.openfreemap.org/styles/dark';

export const EOX_ATTRIBUTION =
  '<a href="https://cloudless.eox.at" target="_blank" rel="noopener">Sentinel-2 cloudless 2025</a> by EOX IT Services GmbH (contains modified Copernicus Sentinel data 2025), CC BY-NC-SA 4.0';

/** Boundary layers in OpenFreeMap's dark and fiord styles (research doc 8.2). Any other layer drawn
 *  from the `boundary` source layer is removed as well, in case the style changes. */
const BOUNDARY_LAYER_IDS = new Set(['boundary_state', 'boundary_country_z0-4', 'boundary_country_z5-', 'boundary_3', 'boundary_2', 'boundary_disputed']);
const BAD_REGION_LABELS = ['Azad Kashmir', 'Gilgit-Baltistan', 'Aksai Chin'];
const NAME_FIELDS = ['name', 'name_en', 'name:en', 'name:latin', 'name_int'];

type RGBA = [number, number, number, number];

function parseColor(input: string): RGBA | null {
  const s = input.trim();
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(s);
  if (m) {
    const h = m[1].length === 3 ? m[1].split('').map((c) => c + c).join('') : m[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
  }
  m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(s);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] != null ? Number(m[4]) : 1];
  m = /^hsla?\(\s*([\d.]+)(?:deg)?\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(s);
  if (m) {
    const h = Number(m[1]) / 360;
    const sat = Number(m[2]) / 100;
    const l = Number(m[3]) / 100;
    const f = (n: number) => {
      const k = (n + h * 12) % 12;
      return l - sat * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    };
    return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255), m[4] != null ? Number(m[4]) : 1];
  }
  return null;
}

const css = ([r, g, b, a]: RGBA) => (a < 1 ? `rgba(${r},${g},${b},${a})` : `rgb(${r},${g},${b})`);
const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(v)));

/** A near-neutral grey moved toward the palette's warm greys (#0c0a08, #181512, #8f8a84 sit about
 *  +3 red and -4 blue from neutral). Coloured values are left alone. */
function warm(color: string): string {
  const c = parseColor(color);
  if (!c) return color;
  const [r, g, b, a] = c;
  if (Math.max(r, g, b) - Math.min(r, g, b) > 12) return color;
  const v = (r + g + b) / 3;
  return css([clamp(v + 2 + 0.02 * v), clamp(v), clamp(v - 2 - 0.035 * v), a]);
}

/** Walk a paint value (a colour string or an expression) and warm every colour in it. */
function warmValue(v: unknown): unknown {
  if (typeof v === 'string') return warm(v);
  if (Array.isArray(v)) return v.map(warmValue);
  return v;
}

function warmPaint(layer: LayerSpecification): LayerSpecification {
  if (!('paint' in layer) || !layer.paint) return layer;
  const paint: Record<string, unknown> = { ...(layer.paint as Record<string, unknown>) };
  for (const k of Object.keys(paint)) if (k.endsWith('-color')) paint[k] = warmValue(paint[k]);
  return { ...layer, paint } as LayerSpecification;
}

const sourceLayer = (l: LayerSpecification): string | undefined => ('source-layer' in l ? (l['source-layer'] as string | undefined) : undefined);

/** Hide the region labels that depict a non-Indian claim, whatever name field the tiles use. */
function withoutBadLabels(layer: LayerSpecification): LayerSpecification {
  if (layer.type !== 'symbol' || sourceLayer(layer) !== 'place') return layer;
  const bad = ['any', ...NAME_FIELDS.map((f) => ['in', ['coalesce', ['get', f], ''], ['literal', BAD_REGION_LABELS]])];
  const filter = layer.filter ? ['all', layer.filter, ['!', bad]] : ['!', bad];
  return { ...layer, filter } as LayerSpecification;
}

/** Place labels in the page's greys: cities in the muted ink, smaller places a step darker. */
function placeLabelColours(layer: LayerSpecification): LayerSpecification {
  if (layer.type !== 'symbol' || sourceLayer(layer) !== 'place') return layer;
  const minor = /village|other|suburb/.test(layer.id);
  return {
    ...layer,
    paint: { ...(layer.paint ?? {}), 'text-color': minor ? '#6f6a63' : '#8f8a84', 'text-halo-color': 'rgba(12,10,8,0.85)', 'text-halo-width': 1 },
  } as LayerSpecification;
}

const anchor = (id: string): LayerSpecification => ({ id, type: 'background', layout: { visibility: 'none' }, paint: { 'background-color': MARK.ground } });

function addAnchorsAndImagery(layers: LayerSpecification[], imagery: boolean): LayerSpecification[] {
  const out = layers.slice();
  // Imagery: right above the background; the vector fills are hidden so the photograph shows.
  const bgIndex = out.findIndex((l) => l.type === 'background');
  out.splice(bgIndex + 1, 0, {
    id: 'bs-eox-s2',
    type: 'raster',
    source: 'bs-eox-s2',
    layout: { visibility: imagery ? 'visible' : 'none' },
    paint: { 'raster-opacity': 0.92 },
  });
  const firstPlace = out.findIndex((l) => l.type === 'symbol' && sourceLayer(l) === 'place');
  const at = firstPlace < 0 ? out.length : firstPlace;
  out.splice(at, 0, anchor(DATA_ANCHOR), anchor(BOUNDARY_ANCHOR));
  out.push(anchor(TOP_ANCHOR));
  return out.map((l) => {
    if (!imagery || l.type !== 'fill') return l;
    return { ...l, layout: { ...(l.layout ?? {}), visibility: 'none' } } as LayerSpecification;
  });
}

/** OpenFreeMap's style, made India-compliant and warm. Pure: returns a new object.
 *  `foreignLabels: 'dim'` starts the style in the state before the Indian states are known (see
 *  `withForeignLabelsHeld`): no full-size place label anywhere, cities only, small and dim. */
export function indiaCompliantStyle(input: StyleSpecification, opts: { imagery?: boolean; foreignLabels?: 'show' | 'dim' } = {}): StyleSpecification {
  let layers = input.layers
    .filter((l) => !BOUNDARY_LAYER_IDS.has(l.id) && sourceLayer(l) !== 'boundary')
    .map(withoutBadLabels)
    .map(warmPaint)
    .map(placeLabelColours)
    .map((l) => (l.type === 'background' ? ({ ...l, paint: { ...(l.paint ?? {}), 'background-color': MARK.ground } } as LayerSpecification) : l));
  if (opts.foreignLabels === 'dim') layers = withForeignLabelsHeld(layers);
  return {
    ...input,
    sources: {
      ...input.sources,
      'bs-eox-s2': {
        type: 'raster',
        tiles: ['https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2025_3857/default/g/{z}/{y}/{x}.jpg'],
        tileSize: 256,
        maxzoom: 14,
        attribution: EOX_ATTRIBUTION,
      },
    },
    layers: addAnchorsAndImagery(layers, !!opts.imagery),
  };
}

/** A plain ground with the anchors, for when the basemap service is unreachable. */
export function fallbackStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': MARK.ground } }, anchor(DATA_ANCHOR), anchor(BOUNDARY_ANCHOR), anchor(TOP_ANCHOR)],
  };
}

// ---------------------------------------------------------------------------------------------
// labels outside India

type Pos = [number, number];
type Ring2 = Pos[];
export type LabelMask = { type: 'MultiPolygon'; coordinates: Ring2[][] };

/** Douglas-Peucker on one ring, tolerance in degrees (iterative, keeps the ring closed). */
function simplifyRing(ring: Ring2, tol: number): Ring2 {
  if (ring.length <= 4) return ring;
  const keep = new Uint8Array(ring.length);
  keep[0] = 1;
  keep[ring.length - 1] = 1;
  const stack: [number, number][] = [[0, ring.length - 1]];
  const t2 = tol * tol;
  while (stack.length) {
    const [a, b] = stack.pop() as [number, number];
    const [ax, ay] = ring[a];
    const [bx, by] = ring[b];
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    let worst = -1;
    let at = -1;
    for (let i = a + 1; i < b; i++) {
      const [px, py] = ring[i];
      let d2: number;
      if (len2 === 0) d2 = (px - ax) ** 2 + (py - ay) ** 2;
      else {
        const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
        d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2;
      }
      if (d2 > worst) {
        worst = d2;
        at = i;
      }
    }
    if (at > 0 && worst > t2) {
      keep[at] = 1;
      stack.push([a, at], [at, b]);
    }
  }
  return ring.filter((_, i) => keep[i]);
}

/**
 * The Indian states we draw (Survey of India polygons from /api/geo/boundaries?level=state), as one
 * light MultiPolygon for a `within` label filter: outer rings only (a hole there is another Indian
 * state or UT), simplified to about 1 km, which moves no label across a border by more than that.
 * Null when there is nothing usable.
 */
export function labelMaskFrom(fc: { features: readonly { geometry?: { type: string; coordinates: unknown } | null }[] } | null | undefined, tolDeg = 0.01): LabelMask | null {
  const polys: Ring2[][] = [];
  for (const f of fc?.features ?? []) {
    const g = f.geometry;
    if (!g) continue;
    const outers = g.type === 'Polygon' ? [(g.coordinates as Ring2[])[0]] : g.type === 'MultiPolygon' ? (g.coordinates as Ring2[][]).map((p) => p[0]) : [];
    for (const ring of outers) {
      if (!ring || ring.length < 4) continue;
      const s = simplifyRing(ring, tolDeg);
      if (s.length >= 4) polys.push([s]);
    }
  }
  return polys.length ? { type: 'MultiPolygon', coordinates: polys } : null;
}

export const ABROAD_LABEL_LAYER = 'bs-place-abroad';
/** Where a held place layer keeps its own filter while `withForeignLabelsHeld` hides it. */
const OWN_FILTER = 'bs:own-filter';
/** A filter no feature passes (every place feature has a class). */
const NONE = ['==', ['get', 'class'], '__bs_none__'];
const CITY = ['all', ['match', ['geometry-type'], ['Point', 'MultiPoint'], true, false], ['==', ['get', 'class'], 'city']];

const isPlaceLabel = (l: LayerSpecification) => l.type === 'symbol' && sourceLayer(l) === 'place' && l.id !== ABROAD_LABEL_LAYER;
const ownFilter = (l: LayerSpecification): unknown => {
  const meta = ('metadata' in l ? l.metadata : undefined) as Record<string, unknown> | undefined;
  if (meta && OWN_FILTER in meta) return meta[OWN_FILTER] ?? undefined;
  return 'filter' in l ? l.filter : undefined;
};

/** The quiet city layer: Latin script, small, dim (4.6:1 on the ground: the size and case carry
 *  the step down from India's own city names, not unreadable contrast). */
function abroadLayer(city: LayerSpecification, filter: unknown): LayerSpecification | null {
  if (!('source' in city)) return null;
  return {
    id: ABROAD_LABEL_LAYER,
    type: 'symbol',
    source: city.source,
    'source-layer': 'place',
    filter,
    layout: {
      'text-field': ['coalesce', ['get', 'name:latin'], ['get', 'name_en'], ['get', 'name']],
      'text-font': (city.layout as Record<string, unknown> | undefined)?.['text-font'] ?? ['Noto Sans Regular'],
      'text-size': 10,
      'text-max-width': 8,
    },
    paint: { 'text-color': '#7e7972', 'text-halo-color': 'rgba(12,10,8,0.85)', 'text-halo-width': 1 },
  } as unknown as LayerSpecification;
}

/**
 * The labels before the Indian states are known (they arrive from the API after the basemap, and may
 * not arrive at all): every place label layer is held (its own filter kept in its metadata) and only
 * the quiet city layer shows, everywhere. So no country or province name ("PAKISTAN", "PUNJAB
 * پنجاب") and no full-size foreign city ever paints, even for a moment or when the states fail.
 */
export function withForeignLabelsHeld(layers: readonly LayerSpecification[]): LayerSpecification[] {
  const places = layers.filter(isPlaceLabel);
  const city = places.find((l) => /city/.test(l.id)) ?? places[0];
  const abroad = city ? abroadLayer(city, CITY) : null;
  const out: LayerSpecification[] = [];
  for (const l of layers) {
    if (l.id === ABROAD_LABEL_LAYER) continue;
    if (abroad && l === places[0]) out.push(abroad);
    if (!isPlaceLabel(l)) {
      out.push(l);
      continue;
    }
    const meta = { ...((('metadata' in l ? l.metadata : undefined) as Record<string, unknown> | undefined) ?? {}), [OWN_FILTER]: ownFilter(l) ?? null };
    out.push({ ...l, filter: NONE, metadata: meta } as LayerSpecification);
  }
  return out;
}

/**
 * How to keep the basemap's place labels to India: every `place` label layer gets `within` the
 * mask added to its own filter, and one quiet layer shows only the cities outside it (Lahore,
 * Faisalabad), in Latin script, smaller and dim, so the far side of the border keeps its bearings
 * without its country and province names standing in the frame. No boundary is drawn from this.
 * `abroadFilter` is set on the quiet layer when the style already has it (a held style).
 */
export function foreignLabelPlan(layers: readonly LayerSpecification[], mask: LabelMask): { filters: { id: string; filter: unknown }[]; abroad: LayerSpecification | null; abroadFilter: unknown; beforeId: string | null } {
  const places = layers.filter(isPlaceLabel);
  const inside = ['within', mask];
  const filters = places.map((l) => {
    const own = ownFilter(l);
    return { id: l.id, filter: own ? ['all', own, inside] : inside };
  });
  const abroadFilter = ['all', CITY, ['!', inside]];
  const city = places.find((l) => /city/.test(l.id)) ?? places[0];
  const abroad = city ? abroadLayer(city, abroadFilter) : null;
  return { filters, abroad, abroadFilter, beforeId: places[0]?.id ?? null };
}

/** True if a style still contains anything drawn from a boundary source layer (the lab checks this). */
export function hasBasemapBoundaries(style: StyleSpecification): boolean {
  return style.layers.some((l) => BOUNDARY_LAYER_IDS.has(l.id) || sourceLayer(l) === 'boundary');
}
