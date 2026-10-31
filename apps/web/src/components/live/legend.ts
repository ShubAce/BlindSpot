/**
 * The key for each of mission control's layers, built from the same style objects the layer
 * factories draw with (components/map/palette.ts), so a swatch is always the mark it explains.
 * Kept apart from components/map/registry.ts because the registry imports deck.gl, and this file is
 * read by the parts of the screen that render before the map chunk loads.
 */

import type { LegendItem } from '@/components/map/marks';
import {
  AIR_STYLE,
  FIRE_STYLE,
  fireRadiusPx,
  MARK,
  NAQI_BANDS,
  rgba,
  RISK_RAMP,
  RISK_STYLE,
  SATELLITE_STYLE,
  SCAR_STYLE,
  SMOKE_STYLE,
  WIND_STYLE,
} from '@/components/map/palette';

import type { LiveLayer } from './live-state';

export const BACK_STYLE = {
  line: rgba(MARK.smoke, 200),
  line2: rgba(MARK.smoke, 110),
  cone: rgba(MARK.smoke, 26),
};

export type LayerKey = { label: string; legend: LegendItem[] };

/** What the key reads from the layers' own responses (the wind level, the back run's span), so it
 *  never states a level or a window the API did not send. */
export type LayerKeyMeta = { windLevel?: string | null; backHours?: number | null };

/** "925hPa" -> "925 hPa" */
export const levelText = (level: string | null | undefined): string | null => (level ? level.replace(/^(\d+)\s*hPa$/i, '$1 hPa') : null);

export function layerKey(layer: LiveLayer, city = 'Delhi', meta: LayerKeyMeta = {}): LayerKey {
  switch (layer) {
    case 'fires':
      return {
        label: 'Fires, 24 h',
        legend: [
          { label: 'Last 6 h', mark: { kind: 'dot', fill: FIRE_STYLE.fill, stroke: FIRE_STYLE.stroke, strokeWidth: FIRE_STYLE.strokePx, radius: 4 } },
          { label: '6 to 24 h ago', mark: { kind: 'dot', fill: FIRE_STYLE.fillOld, stroke: FIRE_STYLE.stroke, strokeWidth: FIRE_STYLE.strokePx, radius: 4 } },
          { label: 'Dot area: fire power, 2 to 40 MW', mark: { kind: 'dot', fill: FIRE_STYLE.fill, radius: fireRadiusPx(40) } },
        ],
      };
    case 'scars':
      return {
        label: 'Burn scars',
        legend: [
          { label: 'Candidate unseen burn', mark: { kind: 'area', fill: SCAR_STYLE.unseenFill, stroke: SCAR_STYLE.unseenStroke, strokeWidth: SCAR_STYLE.strokePx } },
          { label: 'Burn with a fire alert', mark: { kind: 'area', fill: SCAR_STYLE.alertedFill, stroke: SCAR_STYLE.alertedStroke, strokeWidth: SCAR_STYLE.strokePx } },
          { label: 'Not yet checked against fire alerts', mark: { kind: 'outline', stroke: SCAR_STYLE.uncheckedStroke, strokeWidth: SCAR_STYLE.strokePx, fill: SCAR_STYLE.uncheckedFill } },
        ],
      };
    case 'risk':
      return {
        label: 'Harvested, unburnt',
        legend: [0, 2, 4].map((i) => {
          const c = RISK_STYLE.ramp[i];
          return {
            label: i === 0 ? 'Lower risk of burning next' : i === 2 ? 'Middle' : 'Highest risk',
            mark: { kind: 'outline' as const, stroke: c, strokeWidth: RISK_STYLE.strokePx, fill: [c[0], c[1], c[2], RISK_STYLE.fillAlpha] as [number, number, number, number] },
          };
        }),
      };
    case 'smoke':
      return { label: 'Smoke forecast', legend: [{ label: 'Smoke from fires, drifting hour by hour (two weather models)', mark: { kind: 'trail', color: SMOKE_STYLE.color, width: 3 } }] };
    case 'smokeBack':
      return {
        label: `Air into ${city}`,
        legend: [
          { label: `Where ${city}'s air came from${meta.backHours ? `, last ${meta.backHours} h` : ''}`, mark: { kind: 'trail', color: SMOKE_STYLE.color, width: 3 } },
          { label: 'Centre line of each weather model', mark: { kind: 'line', color: BACK_STYLE.line, width: 2 } },
          { label: 'Spread between the models', mark: { kind: 'area', fill: BACK_STYLE.cone } },
        ],
      };
    case 'wind':
      return { label: 'Wind', legend: [{ label: levelText(meta.windLevel) ? `Model wind at ${levelText(meta.windLevel)}` : 'Model wind', mark: { kind: 'trail', color: WIND_STYLE.color, width: 2 } }] };
    case 'air':
      return {
        label: 'Air stations',
        legend: [
          ...[0, 2, 3, 5].map((i) => ({ label: `PM2.5, ${NAQI_BANDS[i]}`, mark: { kind: 'dot' as const, fill: AIR_STYLE.ramp[i], stroke: AIR_STYLE.stroke, strokeWidth: AIR_STYLE.strokePx, radius: AIR_STYLE.radiusPx } })),
          { label: 'Model estimate, not measured', mark: { kind: 'ring', stroke: AIR_STYLE.ramp[3], strokeWidth: AIR_STYLE.strokePx, radius: AIR_STYLE.radiusPx } },
        ],
      };
    case 'satellites':
      return {
        label: 'Satellites',
        legend: [
          { label: 'Satellite now', mark: { kind: 'dot', fill: SATELLITE_STYLE.dot, stroke: SATELLITE_STYLE.dotStroke, strokeWidth: 1.5, radius: SATELLITE_STYLE.dotPx } },
          { label: 'Ground track', mark: { kind: 'line', color: SATELLITE_STYLE.track, width: SATELLITE_STYLE.trackPx } },
          { label: 'Strip being imaged', mark: { kind: 'band', fill: SATELLITE_STYLE.swathFill, edge: SATELLITE_STYLE.swathEdge } },
          { label: 'Next look at Punjab, with its time', mark: { kind: 'band', fill: SATELLITE_STYLE.passFill, edge: SATELLITE_STYLE.passEdge } },
        ],
      };
  }
}

/** One swatch per toggle: the layer's main mark. */
export function toggleSwatch(layer: LiveLayer) {
  return layerKey(layer).legend[0].mark;
}

export { RISK_RAMP };
