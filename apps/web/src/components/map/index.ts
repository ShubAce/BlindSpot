/**
 * The map kit. Pages import from here:
 *
 *   import { BaseMap, firesLayer, scarsLayers, smokeLayers, LAYER_LIST, Legend } from '@/components/map';
 *
 *   const fires = useFires({ hours: 24 });
 *   const layers = useMemo(() => [firesLayer(fires.data, { now })], [fires.data, now]);
 *   <BaseMap layers={layers} className="h-[70svh]" />
 *
 * BaseMap is a client component; build layers inside client components (useMemo on the data).
 * To follow the site's colour theme, read it with useTheme() (lib/theme.ts) and pass it to BaseMap
 * (`theme`) and to every factory (`{ theme }`), with the theme in the useMemo's dependencies; a
 * legend reads the same marks from `paletteFor(theme)`. Without it everything draws dark.
 */

export { BaseMap, TOOLTIP_STYLE, type BaseMapProps } from './BaseMap';
export { DeckOverlay, resolveLayers, type TooltipContent } from './DeckOverlay';
export { Legend, LegendSwatch } from './Legend';
export type { LegendItem, Mark } from './marks';
export { mapClock, useAnimationClock, usePrefersReducedMotion, type ClockFrame } from './clock';
export { BASE_ATTRIBUTION, describePick, LAYER_LIST, LAYER_ORDER, LAYERS, layerLegend, layerOrder, registryIdOf, type LayerDescriptor, type MapLayerId } from './registry';
export { DATA_ANCHOR, BOUNDARY_ANCHOR, TOP_ANCHOR, isAnimated, type AnimatedLayer, type MapLayerInput } from './types';
export { cssColor, rgba, naqiBand, paletteFor, riskBand, NAQI_BANDS, SCAR_TILE_GEOMETRY, SCAR_TILE_SHARE_EDGES, scarTileStep, type MapPalette, type RGBA } from './palette';
export { flyTo } from './camera';

export { airLayers, describeStation } from './layers/air';
export { describeFire, firesLayer, type FireFeature } from './layers/fires';
export { describeGeoPixel, geostationaryLayer, geostationaryPixels } from './layers/geostationary';
export { imageryLayer } from './layers/imagery';
export { describeRisk, riskLayers, type RiskFeature } from './layers/risk';
export { describeSatellite, satelliteLayers } from './layers/satellites';
export { lookOffsetText, nextLookParts, type NextLook } from './look';
export { describeScar, scarState, scarsLayers, type ScarFeature, type ScarState } from './layers/scars';
export { binScars, describeDistrictTile, describeScarTile, districtNameAt, districtTiles, mercatorMetresPerPixel, scarTilesLayer, type DistrictSeasonRow, type DistrictShape, type ScarTile } from './layers/scar-tiles';
export { smokeLayers, smokeTimeAt } from './layers/smoke';
export { readWindField, sampleWind, windLayer, windStreamlines } from './layers/wind';
