import type { Layer } from '@deck.gl/core';
import { GeoJsonLayer, ScatterplotLayer } from '@deck.gl/layers';

import { centroidOf, type AreaGeometry, type Feature, type LonLat, type ScarProps, type ScarsResponse } from '@/lib/api';
import { dateIST, metricText, number, placeName } from '@/lib/format';
import type { Theme } from '@/lib/theme';

import { paletteFor, type RGBA } from '../palette';
import { DATA_ANCHOR, type AnchorProps } from '../types';

export type ScarFeature = Feature<AreaGeometry, ScarProps>;

export type ScarsLayerOptions = {
  id?: string;
  pickable?: boolean;
  visible?: boolean;
  /** Outline one scar in ink (the case file being read). */
  selectedId?: number | string | null;
  /** Also draw the burns a FIRMS alert explains, in muted ink (default true). */
  showAlerted?: boolean;
  /** Colours for this theme's basemap (default dark). */
  theme?: Theme;
};

/** A polygon's first ring averaged: good enough to place a 2.5 px dot when the API omits `centroid`. */
function ringCentroid(g: AreaGeometry): LonLat | null {
  const ring = g.type === 'Polygon' ? g.coordinates[0] : g.coordinates[0]?.[0];
  if (!ring?.length) return null;
  let x = 0;
  let y = 0;
  for (const p of ring) {
    x += p[0];
    y += p[1];
  }
  return [x / ring.length, y / ring.length];
}

const isSelected = (f: ScarFeature, id: ScarsLayerOptions['selectedId']) => id != null && String(f.properties.id) === String(id);

/** The three honest states of a scar: `unseen` is null until the fire-matching job has run on it. */
export type ScarState = 'unseen' | 'alerted' | 'unchecked';
export const scarState = (p: ScarProps): ScarState => (p.unseen === true ? 'unseen' : p.unseen === false ? 'alerted' : 'unchecked');

/** Draw order: unchecked first, then alerted, unseen on top. */
const ORDER: Record<ScarState, number> = { unchecked: 0, alerted: 1, unseen: 2 };

/**
 * Sentinel-2 burn scars from /api/scars. Candidate unseen burns (no fire alert explains them) are
 * filled in gold; burns an alert explains are filled in muted ink; scars not yet checked against the
 * alerts get only a muted outline, so they claim neither. A field is about 1-4 ha, smaller than a
 * pixel when zoomed out, so each also gets a 2.5 px dot at its centroid, drawn beneath the polygon,
 * which covers it up close.
 */
export function scarsLayers(data: ScarsResponse | null | undefined, opts: ScarsLayerOptions = {}): Layer[] {
  const all = (data?.features?.features ?? []).filter((f) => f?.geometry && f.properties);
  const features = (opts.showAlerted ?? true ? all : all.filter((f) => f.properties.unseen === true))
    .slice()
    .sort((a, b) => ORDER[scarState(a.properties)] - ORDER[scarState(b.properties)]);
  if (!features.length) return [];
  const id = opts.id ?? 'scars';
  const { SCAR_STYLE, HIGHLIGHT, theme } = paletteFor(opts.theme);
  const FILL: Record<ScarState, RGBA> = { unseen: SCAR_STYLE.unseenFill, alerted: SCAR_STYLE.alertedFill, unchecked: SCAR_STYLE.uncheckedFill };
  const STROKE: Record<ScarState, RGBA> = { unseen: SCAR_STYLE.unseenStroke, alerted: SCAR_STYLE.alertedStroke, unchecked: SCAR_STYLE.uncheckedStroke };
  const fill = (f: ScarFeature) => FILL[scarState(f.properties)];
  const dots = features
    .map((f) => ({ f, c: centroidOf(f.properties.centroid) ?? ringCentroid(f.geometry) }))
    .filter((d): d is { f: ScarFeature; c: LonLat } => d.c != null);

  return [
    new ScatterplotLayer<{ f: ScarFeature; c: LonLat }, AnchorProps>({
      id: `${id}:dots`,
      beforeId: DATA_ANCHOR,
      data: dots,
      visible: opts.visible ?? true,
      pickable: opts.pickable ?? true,
      radiusUnits: 'pixels',
      getPosition: (d) => d.c,
      getRadius: SCAR_STYLE.dotPx,
      getFillColor: (d) => fill(d.f),
      // deck.gl never compares accessor functions: the theme is what tells it the colours changed
      updateTriggers: { getFillColor: [theme] },
    }),
    new GeoJsonLayer<ScarProps, AnchorProps>({
      id: `${id}:fields`,
      beforeId: DATA_ANCHOR,
      data: features,
      visible: opts.visible ?? true,
      pickable: opts.pickable ?? true,
      autoHighlight: true,
      highlightColor: HIGHLIGHT,
      filled: true,
      stroked: true,
      lineWidthUnits: 'pixels',
      getFillColor: (f) => FILL[scarState(f.properties)],
      getLineColor: (f) =>
        isSelected(f as ScarFeature, opts.selectedId) ? SCAR_STYLE.selectedStroke : STROKE[scarState(f.properties)],
      getLineWidth: (f) => (isSelected(f as ScarFeature, opts.selectedId) ? SCAR_STYLE.selectedStrokePx : SCAR_STYLE.strokePx),
      updateTriggers: { getFillColor: [theme], getLineColor: [opts.selectedId ?? null, theme], getLineWidth: [opts.selectedId ?? null] },
    }),
  ];
}

/** Tooltip text for one scar (from either sublayer). */
export function describeScar(f: ScarFeature): string {
  const p = f.properties;
  const state = scarState(p);
  // the API's own label when it sends one ("candidate unseen burn", "candidate burn, FIRMS-confirmed",
  // "candidate burn (no fire-record verdict yet)"), word for word with a capital first letter
  const apiLabel = (p as ScarProps & { label?: string | null }).label?.trim();
  const head = apiLabel
    ? apiLabel[0].toUpperCase() + apiLabel.slice(1)
    : state === 'unseen' ? 'Candidate unseen burn' : state === 'alerted' ? 'Burn with a fire alert' : 'Burn not yet checked against fire alerts';
  const fires =
    state === 'unchecked'
      ? null
      : p.fire_matches
        ? `${number(p.fire_matches)} fire alert${p.fire_matches === 1 ? '' : 's'} within reach`
        : state === 'unseen'
          ? 'No fire alert explains it'
          : null;
  return [
    head,
    `${metricText('area_ha', p.area_ha)}, burnt between ${dateIST(p.burn_after)} and ${dateIST(p.burn_before)}`,
    fires,
    placeName(p.village ? `near ${p.village}` : null, p.district),
  ]
    .filter(Boolean)
    .join('\n');
}
