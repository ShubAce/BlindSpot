/**
 * The map's layers as data: id, label, what it shows, where it comes from, how often it refreshes,
 * its credit line, and its legend (built from the same style objects the factories draw with).
 * A layer panel can be generated from `LAYER_LIST` alone; BaseMap uses `layerOrder` to stack layers
 * and `describePick` for tooltips.
 */

import type { PickingInfo } from '@deck.gl/core';

import { PATHS } from '@/lib/api';
import { CADENCE } from '@/lib/cadence';

import type { LegendItem } from './marks';
import {
  AIR_STYLE,
  FIRE_STYLE,
  fireRadiusPx,
  GEO_PIXEL_STYLE,
  NAQI_BANDS,
  NAQI_PM25_EDGES,
  RISK_STYLE,
  SATELLITE_STYLE,
  SCAR_STYLE,
  SMOKE_STYLE,
  WIND_STYLE,
} from './palette';
import { describeStation } from './layers/air';
import { describeFire, type FireFeature } from './layers/fires';
import { describeGeoPixel } from './layers/geostationary';
import { describeRisk, type RiskFeature } from './layers/risk';
import { describeSatellite } from './layers/satellites';
import { describeDistrictTile, describeScarTile, type ScarTile } from './layers/scar-tiles';
import { describeScar, type ScarFeature } from './layers/scars';

export type MapLayerId = 'imagery' | 'risk' | 'scars' | 'geostationary' | 'smoke' | 'wind' | 'air' | 'fires' | 'satellites';

export type LayerDescriptor = {
  id: MapLayerId;
  /** Short name for a toggle. */
  label: string;
  /** One sentence: what the marks mean. */
  description: string;
  /** The API path the data comes from. */
  endpoint: string;
  /** source keys in /api/sources whose freshness this layer depends on */
  sourceKeys: string[];
  /** How often the job behind it refreshes (lib/live.ts CADENCE). */
  cadenceMs: number;
  /** Credit for the map's attribution when the layer is on. */
  attribution: string;
  animated: boolean;
  defaultOn: boolean;
  legend: LegendItem[];
};

const ringFor = (i: number) => RISK_STYLE.ramp[i];

export const LAYERS: Record<MapLayerId, LayerDescriptor> = {
  fires: {
    id: 'fires',
    label: 'Fire alerts',
    description: 'NASA FIRMS detections from VIIRS and MODIS in the last 24 hours; dot area follows fire radiative power.',
    endpoint: PATHS.fires,
    sourceKeys: ['firms'],
    cadenceMs: CADENCE.fires,
    attribution: 'Fire alerts: NASA FIRMS',
    animated: false,
    defaultOn: true,
    legend: [
      { label: 'Last 6 h', mark: { kind: 'dot', fill: FIRE_STYLE.fill, stroke: FIRE_STYLE.stroke, strokeWidth: FIRE_STYLE.strokePx, radius: 4 } },
      { label: '6 to 24 h ago', mark: { kind: 'dot', fill: FIRE_STYLE.fillOld, stroke: FIRE_STYLE.stroke, strokeWidth: FIRE_STYLE.strokePx, radius: 4 } },
      { label: '2 MW', mark: { kind: 'dot', fill: FIRE_STYLE.fill, radius: fireRadiusPx(2) } },
      { label: '40 MW', mark: { kind: 'dot', fill: FIRE_STYLE.fill, radius: fireRadiusPx(40) } },
    ],
  },
  geostationary: {
    id: 'geostationary',
    label: 'Evening fire activity',
    description: 'GK-2A geostationary fire pixels, every 10 minutes; each square is about 4 km across and may hold many fields.',
    endpoint: PATHS.geostationaryLatest,
    sourceKeys: ['gk2a'],
    cadenceMs: CADENCE.geostationary,
    attribution: 'GK-2A: KMA via NOAA Open Data Dissemination',
    animated: false,
    defaultOn: false,
    legend: [{ label: 'GK-2A fire pixel', mark: { kind: 'square', stroke: GEO_PIXEL_STYLE.stroke, fill: GEO_PIXEL_STYLE.fill, strokeWidth: GEO_PIXEL_STYLE.strokePx } }],
  },
  scars: {
    id: 'scars',
    label: 'Burn scars',
    description: 'Fields Sentinel-2 saw burn between two clear passes; gold where no fire alert explains the burn.',
    endpoint: PATHS.scars,
    sourceKeys: ['stac_e84', 'stac_pc', 'firms'],
    cadenceMs: CADENCE.scars,
    attribution: 'Contains modified Copernicus Sentinel-2 data 2026',
    animated: false,
    defaultOn: true,
    legend: [
      { label: 'Candidate unseen burn', mark: { kind: 'area', fill: SCAR_STYLE.unseenFill, stroke: SCAR_STYLE.unseenStroke, strokeWidth: SCAR_STYLE.strokePx } },
      { label: 'Burn with a fire alert', mark: { kind: 'area', fill: SCAR_STYLE.alertedFill, stroke: SCAR_STYLE.alertedStroke, strokeWidth: SCAR_STYLE.strokePx } },
      { label: 'Not yet checked against fire alerts', mark: { kind: 'outline', stroke: SCAR_STYLE.uncheckedStroke, strokeWidth: SCAR_STYLE.strokePx, fill: SCAR_STYLE.uncheckedFill } },
    ],
  },
  risk: {
    id: 'risk',
    label: 'Harvested, not burnt',
    description: 'Fields cut but not yet burnt, ranked by the risk that they burn next; paler is higher.',
    endpoint: PATHS.risk,
    sourceKeys: ['stac_e84', 'stac_pc'],
    cadenceMs: CADENCE.risk,
    attribution: 'Contains modified Copernicus Sentinel-2 data 2026',
    animated: false,
    defaultOn: false,
    legend: [0, 2, 4].map((i) => ({
      label: i === 0 ? 'Lower risk' : i === 2 ? 'Middle' : 'Highest risk',
      mark: { kind: 'outline' as const, stroke: ringFor(i), strokeWidth: RISK_STYLE.strokePx, fill: [ringFor(i)[0], ringFor(i)[1], ringFor(i)[2], RISK_STYLE.fillAlpha] as [number, number, number, number] },
    })),
  },
  smoke: {
    id: 'smoke',
    label: 'Smoke',
    description: 'Where smoke from recent fires and fresh scars drifts, hour by hour, in two forecast models.',
    endpoint: PATHS.smokeForward,
    sourceKeys: ['openmeteo', 'firms'],
    cadenceMs: CADENCE.smoke,
    attribution: 'Winds: Open-Meteo (ECMWF, DWD, NOAA)',
    animated: true,
    defaultOn: true,
    legend: [{ label: 'Smoke trail', mark: { kind: 'trail', color: SMOKE_STYLE.color, width: 3 } }],
  },
  wind: {
    id: 'wind',
    label: 'Wind',
    description: 'Wind at 925 hPa, about 800 m up, the level the smoke rides.',
    endpoint: PATHS.windField,
    sourceKeys: ['openmeteo'],
    cadenceMs: CADENCE.wind,
    attribution: 'Winds: Open-Meteo (ECMWF, DWD, NOAA)',
    animated: true,
    defaultOn: false,
    legend: [{ label: 'Wind', mark: { kind: 'trail', color: WIND_STYLE.color, width: WIND_STYLE.widthPx } }],
  },
  air: {
    id: 'air',
    label: 'Air quality',
    description: 'PM2.5 at CPCB stations, on the six National AQI bands; rings are model estimates.',
    endpoint: PATHS.airLatest,
    sourceKeys: ['openaq', 'datagov_aqi', 'cams'],
    cadenceMs: CADENCE.air,
    attribution: 'Air quality: CPCB via data.gov.in and OpenAQ; model: Copernicus CAMS',
    animated: false,
    defaultOn: true,
    legend: [
      ...NAQI_BANDS.map((band, i) => ({
        label: band,
        note: i < NAQI_PM25_EDGES.length ? `PM2.5 up to ${NAQI_PM25_EDGES[i]} µg/m³ (24 h)` : `PM2.5 above ${NAQI_PM25_EDGES[NAQI_PM25_EDGES.length - 1]} µg/m³ (24 h)`,
        mark: { kind: 'dot' as const, fill: AIR_STYLE.ramp[i], stroke: AIR_STYLE.stroke, strokeWidth: AIR_STYLE.strokePx, radius: AIR_STYLE.radiusPx },
      })),
      { label: 'Model', mark: { kind: 'ring', stroke: AIR_STYLE.ramp[3], strokeWidth: AIR_STYLE.strokePx, radius: AIR_STYLE.radiusPx } },
    ],
  },
  satellites: {
    id: 'satellites',
    label: 'Satellites',
    description: 'Fire satellites and Sentinel-2 where they are now, the ground they are imaging, and their tracks.',
    endpoint: PATHS.orbitsTle,
    sourceKeys: ['celestrak'],
    cadenceMs: CADENCE.orbits,
    attribution: 'Orbits: CelesTrak',
    animated: false,
    defaultOn: true,
    legend: [
      { label: 'Satellite now', mark: { kind: 'dot', fill: SATELLITE_STYLE.dot, stroke: SATELLITE_STYLE.dotStroke, strokeWidth: 1.5, radius: SATELLITE_STYLE.dotPx } },
      { label: 'Ground track', mark: { kind: 'line', color: SATELLITE_STYLE.track, width: SATELLITE_STYLE.trackPx } },
      { label: 'Swath being imaged', mark: { kind: 'band', fill: SATELLITE_STYLE.swathFill, edge: SATELLITE_STYLE.swathEdge } },
    ],
  },
  imagery: {
    id: 'imagery',
    label: 'Sentinel-2 scene',
    description: 'One Sentinel-2 photograph at 10 m; in short-wave infrared, burnt ground reads dark red.',
    endpoint: '/api/imagery/tiles',
    sourceKeys: ['pc_data', 'stac_pc'],
    cadenceMs: CADENCE.imagery,
    attribution: 'Contains modified Copernicus Sentinel-2 data 2026',
    animated: false,
    defaultOn: false,
    legend: [],
  },
};

/** Bottom to top. Pictures under shapes, shapes under trails, points on top, satellites last. */
export const LAYER_ORDER: readonly MapLayerId[] = ['imagery', 'risk', 'scars', 'geostationary', 'smoke', 'wind', 'air', 'fires', 'satellites'];
export const LAYER_LIST: readonly LayerDescriptor[] = LAYER_ORDER.map((id) => LAYERS[id]);

/** Credits always shown on our maps (the basemap's own come from its TileJSON). */
export const BASE_ATTRIBUTION = ['Boundaries: Survey of India', 'Districts: LGD / Bharatmaps via DataMeet'];

/** The registry id a deck.gl layer id belongs to ("scars:fields" -> "scars"). */
export function registryIdOf(layerId: string): MapLayerId | null {
  const head = layerId.split(':')[0] as MapLayerId;
  return head in LAYERS ? head : null;
}

/** Stacking rank for any deck.gl layer id; unknown layers go on top of ours. */
export function layerOrder(layerId: string): number {
  const id = registryIdOf(layerId);
  return id ? LAYER_ORDER.indexOf(id) : LAYER_ORDER.length;
}

/** Tooltip text for a picked object, or null for nothing. */
export function describePick(info: PickingInfo): string | null {
  if (!info.picked || !info.object || !info.layer) return null;
  const o = info.object as Record<string, unknown>;
  switch (registryIdOf(info.layer.id)) {
    case 'fires':
      return describeFire(o as FireFeature);
    case 'scars':
      if ('drawn' in o && 'unseenShare' in o) return o.district ? describeDistrictTile(o as unknown as ScarTile, null) : describeScarTile(o as unknown as ScarTile);
      return describeScar((('f' in o ? o.f : o) as ScarFeature));
    case 'risk':
      return describeRisk((('f' in o ? o.f : o) as RiskFeature));
    case 'air':
      return describeStation(o as Parameters<typeof describeStation>[0]);
    case 'geostationary':
      return describeGeoPixel(o as Parameters<typeof describeGeoPixel>[0]);
    case 'satellites':
      return 'sat' in o && 'heightKm' in o ? describeSatellite(o as Parameters<typeof describeSatellite>[0]) : null;
    default:
      return null;
  }
}
