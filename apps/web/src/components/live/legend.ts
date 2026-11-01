/**
 * The key for each of mission control's layers, built from the same style objects the layer
 * factories draw with (components/map/palette.ts), so a swatch is always the mark it explains. Each
 * takes the theme's palette (`paletteFor(theme)`, dark when none is given): the marks change colour
 * with the basemap under them.
 * Kept apart from components/map/registry.ts because the registry imports deck.gl, and this file is
 * read by the parts of the screen that render before the map chunk loads.
 */

import type { LegendItem } from '@/components/map/marks';
import { fireRadiusPx, NAQI_BANDS, paletteFor, SCAR_TILE_SHARE_EDGES, type MapPalette } from '@/components/map/palette';

import type { LiveLayer } from './live-state';

const DARK = paletteFor('dark');

export type LayerKey = { label: string; legend: LegendItem[] };

/** What the key reads from the layers' own responses (the wind level, the back run's span), so it
 *  never states a level or a window the API did not send. */
export type LayerKeyMeta = { windLevel?: string | null; backHours?: number | null };

/** "925hPa" -> "925 hPa" */
export const levelText = (level: string | null | undefined): string | null => (level ? level.replace(/^(\d+)\s*hPa$/i, '$1 hPa') : null);

export function layerKey(layer: LiveLayer, city = 'Delhi', meta: LayerKeyMeta = {}, palette: MapPalette = DARK): LayerKey {
  const { FIRE_STYLE, SCAR_STYLE, RISK_STYLE, SMOKE_STYLE, BACK_STYLE, WIND_STYLE, AIR_STYLE, SATELLITE_STYLE } = palette;
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
          { label: 'Next look at Punjab: the line its scanner sweeps through it, with the time', mark: { kind: 'line', color: SATELLITE_STYLE.lookLine, width: SATELLITE_STYLE.lookLinePx } },
        ],
      };
  }
}

/**
 * The scars' key at region zoom, where they are binned into hexagon tiles: a ramp of the four
 * colour steps (share of a tile's checked hectares that is candidate unseen burn), then the
 * unchecked outline and what a tile's size means. Same style objects as the tiles themselves.
 */
export function scarTileKey(view: 'districts' | 'tiles' = 'tiles', palette: MapPalette = DARK): { rampTitle: string; ramp: LegendItem[]; legend: LegendItem[] } {
  const { SCAR_TILE_STYLE } = palette;
  const edges = SCAR_TILE_SHARE_EDGES.map((e) => Math.round(e * 100));
  const steps = SCAR_TILE_STYLE.steps.map((fill, i) => ({
    label: i === 0 ? `under ${edges[0]}%` : i === edges.length ? `${edges[i - 1]}% or more` : `${edges[i - 1]}–${edges[i]}%`,
    mark: { kind: 'hex' as const, fill },
  }));
  if (view === 'districts') {
    return {
      rampTitle: 'Share of each district’s checked hectares that is candidate unseen burn, this season',
      ramp: steps,
      legend: [{ label: 'Size: hectares burnt this season; click a district for its fields', mark: { kind: 'hex', fill: SCAR_TILE_STYLE.steps[3], scale: 0.6 } }],
    };
  }
  return {
    rampTitle: 'Share of each tile’s checked hectares that is candidate unseen burn',
    ramp: steps,
    legend: [
      { label: 'Fields not yet checked against fire alerts', mark: { kind: 'hex', fill: SCAR_TILE_STYLE.uncheckedFill, stroke: SCAR_TILE_STYLE.uncheckedStroke, strokeWidth: 1 } },
      { label: 'Tile size: hectares burnt; zoom in or click for the fields', mark: { kind: 'hex', fill: SCAR_TILE_STYLE.steps[3], scale: 0.6 } },
    ],
  };
}

/** One swatch per toggle: the layer's main mark. */
export function toggleSwatch(layer: LiveLayer, palette: MapPalette = DARK) {
  return layerKey(layer, undefined, undefined, palette).legend[0].mark;
}

