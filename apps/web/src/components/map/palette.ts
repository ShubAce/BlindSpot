/**
 * Colours and mark styles for everything drawn on the map. Each layer factory and its legend read
 * the same style object, so a legend swatch is always the mark itself, scaled down.
 *
 * Choices (on the warm-black basemap, #0c0a08), checked with the dataviz skill's validator
 * (Machado 2009 CVD simulation at severity 1, OKLab ΔE x100, WCAG contrast):
 * - Fires #ea5a2e, a warm orange-red. Candidate unseen burns #c9a45a, the page's gold accent.
 *   Together, all pairs: deuteranopia ΔE 8.1 (target 8), protanopia higher, tritanopia 17.9,
 *   normal vision 16.3 (floor 15). Contrast on the ground: fire 5.65:1, gold 8.41:1.
 *   Gold's OKLCH lightness (0.737) is above the validator's dark band (0.48-0.67); kept, because it
 *   is the direction's accent and reads clearly.
 * - Burns a fire alert explains #7d776f, muted ink (a neutral on purpose, 4.46:1): the seen burns
 *   recede and the unseen ones carry the story. Gold vs this grey: normal-vision ΔE above 16.
 * - Smoke: warm white #fbf8f3 at low alpha. Wind: #d9d2c8, thinner and fainter still.
 * - Next-burn risk: one warm ramp (straw, the colour harvested parali reads from space), five steps
 *   from #5b5141 to #f3dfa6. Ordinal checks pass: lightness monotone, every step at least 0.06 apart,
 *   one hue (12 degree spread), the dark end 2.54:1 on the ground.
 * - Air quality: one neutral lightness ramp over the six CPCB NAQI bands, #544d45 to #fbf8f3
 *   (monotone, single hue, dark end 2.38:1). Worse air is whiter, like the smoke that causes it.
 * No pair relies on hue alone: fires are dots, burns are filled shapes, risk fields are outlines,
 * geostationary pixels are hollow squares, smoke and wind are moving trails, stations carry their
 * value as a label.
 */

export type RGBA = [number, number, number, number];

export function rgba(hex: string, alpha = 255): RGBA {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  return [parseInt(full.slice(0, 2), 16), parseInt(full.slice(2, 4), 16), parseInt(full.slice(4, 6), 16), alpha];
}

export const cssColor = ([r, g, b, a]: RGBA): string => (a >= 255 ? `rgb(${r} ${g} ${b})` : `rgb(${r} ${g} ${b} / ${+(a / 255).toFixed(3)})`);

/** Keep equal to the --mark-* tokens in app/globals.css (the lab page checks they match). */
export const MARK = {
  ground: '#0c0a08',
  ink: '#ffffff',
  fire: '#ea5a2e',
  unseen: '#c9a45a',
  alerted: '#7d776f',
  smoke: '#fbf8f3',
  straw: '#f3dfa6',
  wind: '#d9d2c8',
  satellite: '#f4efe8',
  boundaryState: '#8f8a84',
  boundaryDistrict: '#3d372f',
} as const;

/** Low to high risk score (0-1, five equal bands). */
export const RISK_RAMP = ['#5b5141', '#83744f', '#ab9761', '#d2bc7c', '#f3dfa6'] as const;

/** CPCB National AQI bands, Good to Severe. */
export const AIR_RAMP = ['#544d45', '#6f685f', '#8d857b', '#aca49a', '#d1cac0', '#fbf8f3'] as const;
export const NAQI_BANDS = ['Good', 'Satisfactory', 'Moderate', 'Poor', 'Very poor', 'Severe'] as const;
/** PM2.5 band upper edges in µg/m³ (24-hour, CPCB NAQI). */
export const NAQI_PM25_EDGES = [30, 60, 90, 120, 250] as const;
/** The same bands as a sub-index (data.gov.in publishes CPCB sub-indices, not concentrations). */
export const NAQI_INDEX_EDGES = [50, 100, 200, 300, 400] as const;

/** Which NAQI band a PM2.5 value falls in, honouring the unit the API reports. */
export function naqiBand(value: number, unit?: string | null): number {
  const edges = unit && /index|aqi/i.test(unit) ? NAQI_INDEX_EDGES : NAQI_PM25_EDGES;
  const i = edges.findIndex((e) => value <= e);
  return i === -1 ? edges.length : i;
}

/** Risk band 0-4 for a 0-1 score. */
export const riskBand = (score: number): number => Math.max(0, Math.min(4, Math.floor(score * 5)));

// ---------------------------------------------------------------------------------------------
// one style object per kind of mark

export const FIRE_STYLE = {
  fill: rgba(MARK.fire, 240),
  /** Detections older than six hours stay, dimmer: the map shows when, not only where. */
  fillOld: rgba(MARK.fire, 130),
  stroke: rgba(MARK.ground, 220),
  strokePx: 1,
  minRadiusPx: 2.5,
  maxRadiusPx: 9,
  recentHours: 6,
};

/** Dot radius for a fire radiative power: square-root scale, 1 MW at the minimum, 64 MW and up at the maximum. */
export function fireRadiusPx(frpMw: number | null | undefined): number {
  const f = Math.max(1, Math.min(64, frpMw ?? 1));
  return FIRE_STYLE.minRadiusPx + ((Math.sqrt(f) - 1) / 7) * (FIRE_STYLE.maxRadiusPx - FIRE_STYLE.minRadiusPx);
}

export const GEO_PIXEL_STYLE = {
  stroke: rgba(MARK.fire, 230),
  fill: rgba(MARK.fire, 28),
  strokePx: 1.5,
  /** GK-2A pixels over Punjab are about 4.5 by 3.6 km (research doc, geostationary section 3). */
  halfSizeKm: 2,
};

export const SCAR_STYLE = {
  unseenFill: rgba(MARK.unseen, 235),
  unseenStroke: rgba(MARK.unseen, 255),
  alertedFill: rgba(MARK.alerted, 190),
  alertedStroke: rgba(MARK.alerted, 255),
  /** Not yet checked against fire alerts: the same muted ink, outline only, so it claims neither. */
  uncheckedFill: rgba(MARK.alerted, 40),
  uncheckedStroke: rgba(MARK.alerted, 200),
  selectedStroke: rgba(MARK.ink, 255),
  strokePx: 1,
  selectedStrokePx: 2,
  /** Dots at the centroid keep sub-pixel fields visible when zoomed out; the polygon covers them up close. */
  dotPx: 2.5,
};

/**
 * Scars binned into hexagons at region zoom (layers/scar-tiles.ts). Colour: the share of a tile's
 * checked hectares that no fire alert explains, in four ordinal steps on the line from the grey of a
 * burn with an alert (#7d776f) to the gold of a candidate unseen burn (#c9a45a), so a tile reads
 * like the fields inside it. Lightness rises with the share (one hue family, no rainbow).
 */
// The edges sit where the data varies: across the season's districts the share runs from about 60%
// (Tarn Taran, where FIRMS sees the most fires) to 100%, and at even quarters every tile fell in the
// top step. The key prints these edges, so they are a drawing choice, never a figure.
export const SCAR_TILE_SHARE_EDGES = [0.7, 0.8, 0.9] as const;
export const SCAR_TILE_RAMP = ['#7d776f', '#968668', '#b09561', '#c9a45a'] as const;
export const SCAR_TILE_STYLE = {
  steps: SCAR_TILE_RAMP.map((c) => rgba(c, 235)),
  stroke: rgba(MARK.ground, 0),
  uncheckedFill: rgba(MARK.alerted, 40),
  uncheckedStroke: rgba(MARK.alerted, 200),
  /** Hexagon radius on screen, px (about 25 px across). */
  radiusPx: 14,
  /** A full tile is drawn at this fraction of its cell, so neighbours never touch. */
  gap: 0.92,
  /** The smallest tile, as a fraction of a full one (sqrt of hectares below that clamps here). */
  minScale: 0.45,
  /** Below this zoom the map shows tiles; at and above it, the fields. */
  fieldsZoom: 9,
  /** Below this zoom (live, not replaying) the map shows the season by district instead. */
  districtZoom: 8,
  /** A district's hexagon cell, km of ground (the largest district by hectares fills it to `gap`;
   *  22 km keeps Amritsar and Tarn Taran, the closest large pair, apart), and a floor in px so it
   *  stays a mark when zoomed out; smaller districts scale by the square root of hectares. */
  districtRadiusKm: 22,
  districtMinPx: 9,
  districtMinScale: 0.2,
};
/** Which of the four steps a share falls in. */
export const scarTileStep = (share: number): number => {
  const i = SCAR_TILE_SHARE_EDGES.findIndex((e) => share < e);
  return i === -1 ? SCAR_TILE_SHARE_EDGES.length : i;
};

export const RISK_STYLE = {
  ramp: RISK_RAMP.map((c) => rgba(c, 255)),
  fillAlpha: 34,
  strokePx: 1.5,
  ringPx: 3,
  selectedStroke: rgba(MARK.ink, 255),
};

export const SMOKE_STYLE = {
  color: rgba(MARK.smoke, 150),
  minWidthPx: 1.5,
  maxWidthPx: 4,
  /** The cone between the two forecast models (SYNTHESIS.md: never one path as truth). */
  coneFill: rgba(MARK.smoke, 22),
  trailHours: 3,
  loopSeconds: 18,
};

export const WIND_STYLE = {
  color: rgba(MARK.wind, 110),
  widthPx: 1,
  loopSeconds: 8,
};

export const AIR_STYLE = {
  ramp: AIR_RAMP.map((c) => rgba(c, 255)),
  stroke: rgba(MARK.ground, 255),
  radiusPx: 5,
  strokePx: 1.5,
  labelColor: rgba(MARK.ink, 235),
  labelOutline: rgba(MARK.ground, 255),
  labelPx: 11,
};

export const SATELLITE_STYLE = {
  track: rgba(MARK.satellite, 150),
  trackImaging: rgba(MARK.satellite, 80), // Sentinel-2 photographs, it does not count fires
  trackPx: 1.25,
  swathFill: rgba(MARK.satellite, 14),
  swathEdge: rgba(MARK.satellite, 70),
  passFill: rgba(MARK.satellite, 10),
  passEdge: rgba(MARK.satellite, 110),
  /** The next look at a site: the line its scanner sweeps through the site at closest approach. */
  lookLine: rgba(MARK.satellite, 215),
  lookLinePx: 1.5,
  /** That pass's ground track, either side of the moment it looks. */
  lookTrack: rgba(MARK.satellite, 170),
  lookTrackPx: 1.5,
  dot: rgba(MARK.satellite, 255),
  dotStroke: rgba(MARK.ground, 255),
  dotPx: 4,
  label: rgba(MARK.satellite, 230),
};

export const BOUNDARY_STYLE = {
  state: MARK.boundaryState,
  district: MARK.boundaryDistrict,
};

/** deck.gl autoHighlight: a translucent ink wash, never the library's default blue. */
export const HIGHLIGHT: RGBA = [255, 255, 255, 70];
