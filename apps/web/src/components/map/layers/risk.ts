import type { Layer } from '@deck.gl/core';
import { GeoJsonLayer, ScatterplotLayer } from '@deck.gl/layers';

import { centroidOf, type AreaGeometry, type Feature, type LonLat, type RiskProps, type RiskResponse } from '@/lib/api';
import { dateIST, metricText, number, placeName } from '@/lib/format';

import { HIGHLIGHT, RISK_STYLE, riskBand, type RGBA } from '../palette';
import { DATA_ANCHOR, type AnchorProps } from '../types';

export type RiskFeature = Feature<AreaGeometry, RiskProps>;

export type RiskLayerOptions = { id?: string; pickable?: boolean; visible?: boolean; selectedId?: number | string | null };

const colourFor = (score: number): RGBA => RISK_STYLE.ramp[riskBand(score)];
const withAlpha = ([r, g, b]: RGBA, a: number): RGBA => [r, g, b, a];

/**
 * Harvested fields not yet burnt (/api/risk), ranked by risk score: outlines on one straw ramp
 * (pale = highest), never filled, so they cannot be mistaken for burn scars. Zoomed out, a ring at
 * the centroid stands in for each field.
 */
export function riskLayers(data: RiskResponse | null | undefined, opts: RiskLayerOptions = {}): Layer[] {
  const features = (data?.features?.features ?? [])
    .filter((f) => f?.geometry && Number.isFinite(f.properties?.risk_score))
    // highest risk last, so it draws on top
    .slice()
    .sort((a, b) => a.properties.risk_score - b.properties.risk_score);
  if (!features.length) return [];
  const id = opts.id ?? 'risk';
  const rings = features
    .map((f) => ({ f, c: centroidOf(f.properties.centroid) }))
    .filter((d): d is { f: RiskFeature; c: LonLat } => d.c != null);
  const selected = (f: { properties: RiskProps }) => opts.selectedId != null && String(f.properties.id) === String(opts.selectedId);

  return [
    new ScatterplotLayer<{ f: RiskFeature; c: LonLat }, AnchorProps>({
      id: `${id}:rings`,
      beforeId: DATA_ANCHOR,
      data: rings,
      visible: opts.visible ?? true,
      pickable: opts.pickable ?? true,
      radiusUnits: 'pixels',
      lineWidthUnits: 'pixels',
      filled: false,
      stroked: true,
      getPosition: (d) => d.c,
      getRadius: RISK_STYLE.ringPx,
      getLineColor: (d) => colourFor(d.f.properties.risk_score),
      getLineWidth: RISK_STYLE.strokePx,
    }),
    new GeoJsonLayer<RiskProps, AnchorProps>({
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
      getFillColor: (f) => withAlpha(colourFor(f.properties.risk_score), RISK_STYLE.fillAlpha),
      getLineColor: (f) => (selected(f) ? RISK_STYLE.selectedStroke : colourFor(f.properties.risk_score)),
      getLineWidth: (f) => (selected(f) ? 2 : RISK_STYLE.strokePx),
      updateTriggers: { getLineColor: [opts.selectedId ?? null], getLineWidth: [opts.selectedId ?? null] },
    }),
  ];
}

export function describeRisk(f: RiskFeature): string {
  const p = f.properties;
  return [
    `Harvested, not yet burnt: risk ${number(p.risk_score, { decimals: 2 })}`,
    `${metricText('area_ha', p.area_ha)}${p.days_since_harvest != null ? `, ${number(p.days_since_harvest)} days since harvest` : ''}`,
    p.harvested_after && p.harvested_before ? `Cut between ${dateIST(p.harvested_after)} and ${dateIST(p.harvested_before)}` : null,
    placeName(p.village ? `near ${p.village}` : null, p.district),
  ]
    .filter(Boolean)
    .join('\n');
}
