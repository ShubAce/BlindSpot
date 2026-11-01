/**
 * Colours and mark styles for everything drawn on the map, one set per colour theme. Each layer
 * factory and its legend read the same style object, so a legend swatch is always the mark itself,
 * scaled down. Every colour is read through `paletteFor(theme)` (dark when no theme is given); the
 * constants exported on their own are sizes and zooms, the same on both themes.
 *
 * Dark (on the warm-black basemap, #0c0a08), checked with the dataviz skill's validator (Machado
 * 2009 CVD simulation at severity 1, OKLab ΔE x100, WCAG contrast):
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
 *
 * Light (on the bone basemap, #efeeec; the same rules re-measured, scratch palette study 2026-11-01):
 * - Fire #b82d0f (5.30:1), deeper than dark's: on bone the gold can be no lighter than OKLCH L 0.62
 *   to clear 3:1, and fire must sit about 0.10 below it for deuteranopes to tell them apart. Unseen
 *   gold #a67b01 (3.32:1), the deepest gold that still reads as gold. Alerted #67625c (5.21:1).
 *   All pairs: deuteranopia ΔE 8.8, tritanopia 14.0, normal vision 15.8, every mark 3:1 or more.
 * - Smoke trails turn to a warm grey haze (#57524c): one trail is 4.00:1 at its alpha, and where
 *   hundreds overlap they build to that grey (6.67:1), as smoke reads on a pale sky. Soot (#2b2620)
 *   matched dark's trail (6.79:1 against 6.71:1), but its dense patches piled up to ink and outweighed
 *   the fires. The back run's centre lines and their spread keep the soot (`smokeLine`, at the
 *   alphas set for dark's contrast), and so does wind, thinner and fainter.
 * - Risk runs from a pale straw to the deepest (#aca48c to #5d420e) and air quality from pale grey
 *   to near-black (#a9a49c to #231e19): on light more risk, or worse air, is still more mark.
 *   Both pass the ordinal checks (monotone, steps 0.06 apart, one hue, light end 2.1:1 or more).
 * - Fills that must read stay at 3:1 as drawn (the unseen fill and the tiles go to alpha 250); the
 *   marks that recede on purpose in dark (old fires, the imaging-only track, the district line)
 *   recede by the same amount.
 * No pair relies on hue alone: fires are dots, burns are filled shapes, risk fields are outlines,
 * geostationary pixels are hollow squares, smoke and wind are moving trails, stations carry their
 * value as a label.
 */

import type { Theme } from '@/lib/theme';

export type RGBA = [number, number, number, number];

export function rgba(hex: string, alpha = 255): RGBA {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  return [parseInt(full.slice(0, 2), 16), parseInt(full.slice(2, 4), 16), parseInt(full.slice(4, 6), 16), alpha];
}

export const cssColor = ([r, g, b, a]: RGBA): string => (a >= 255 ? `rgb(${r} ${g} ${b})` : `rgb(${r} ${g} ${b} / ${+(a / 255).toFixed(3)})`);

type MarkSet = {
  /** The map's ground: dot rings, label halos, the basemap's background. */
  ground: string;
  /** Selection and highlight outlines, labels beside them. */
  ink: string;
  fire: string;
  unseen: string;
  alerted: string;
  smoke: string;
  /** The models' centre lines through a back run: the smoke colour, or on light the soot under its haze. */
  smokeLine: string;
  straw: string;
  wind: string;
  satellite: string;
  boundaryState: string;
  boundaryDistrict: string;
};

/** Keep fire, unseen, alerted, smoke and straw equal to the --mark-* tokens in app/globals.css, on
 *  both themes (the lab page checks they match). */
const MARKS: Readonly<Record<Theme, MarkSet>> = {
  dark: {
    ground: '#0c0a08',
    ink: '#ffffff',
    fire: '#ea5a2e',
    unseen: '#c9a45a',
    alerted: '#7d776f',
    smoke: '#fbf8f3',
    smokeLine: '#fbf8f3',
    straw: '#f3dfa6',
    wind: '#d9d2c8',
    satellite: '#f4efe8',
    boundaryState: '#8f8a84',
    boundaryDistrict: '#3d372f',
  },
  light: {
    ground: '#efeeec',
    ink: '#1a1612',
    fire: '#b82d0f',
    unseen: '#a67b01',
    alerted: '#67625c',
    smoke: '#57524c',
    smokeLine: '#2b2620',
    straw: '#5d420e',
    wind: '#2b2620',
    satellite: '#2b2621',
    // the state line at dark's contrast (5.80:1); the district line as quiet as dark's (1.70:1)
    boundaryState: '#605b55',
    boundaryDistrict: '#bdb8b0',
  },
};

/** Low to high risk score (0-1, five equal bands). */
const RISK_RAMPS: Readonly<Record<Theme, readonly string[]>> = {
  dark: ['#5b5141', '#83744f', '#ab9761', '#d2bc7c', '#f3dfa6'],
  light: ['#aca48c', '#988b6b', '#84724c', '#705c2f', '#5d420e'],
};

/** CPCB National AQI bands, Good to Severe. */
const AIR_RAMPS: Readonly<Record<Theme, readonly string[]>> = {
  dark: ['#544d45', '#6f685f', '#8d857b', '#aca49a', '#d1cac0', '#fbf8f3'],
  light: ['#a9a49c', '#8c8680', '#706a64', '#554f49', '#3b3630', '#231e19'],
};

/**
 * Scars binned into hexagons at region zoom (layers/scar-tiles.ts). Colour: the share of a tile's
 * checked hectares that no fire alert explains, in four ordinal steps on the line from the grey of a
 * burn with an alert to the gold of a candidate unseen burn, so a tile reads like the fields inside
 * it. On dark lightness rises with the share; on light it falls while chroma rises (one hue family,
 * no rainbow either way). The light steps sit 0.041 apart in lightness (dark's 0.055): a ramp that
 * clears the validator's 0.06 has to end in a deeper gold, which meets fire under protanopia.
 */
const SCAR_TILE_RAMPS: Readonly<Record<Theme, readonly string[]>> = {
  dark: ['#7d776f', '#968668', '#b09561', '#c9a45a'],
  light: ['#ada8a1', '#ad997a', '#aa8a4f', '#a67b01'],
};

/** Each mark's alpha on dark. */
const DARK_ALPHAS = {
  fire: 240,
  fireOld: 130,
  fireHalo: 220,
  geoStroke: 230,
  geoFill: 28,
  unseenFill: 235,
  unseenStroke: 255,
  alertedFill: 190,
  alertedStroke: 255,
  uncheckedFill: 40,
  uncheckedStroke: 200,
  tile: 235,
  riskFill: 34,
  smoke: 150,
  smokeCone: 22,
  backLine: 200,
  backLine2: 110,
  backCone: 26,
  wind: 110,
  satTrack: 150,
  satImaging: 80,
  swathFill: 14,
  swathEdge: 70,
  passFill: 10,
  passEdge: 110,
  lookLine: 215,
  lookTrack: 170,
  satLabel: 230,
  airLabel: 235,
  highlight: 70,
  hlFill: 40,
  place: 230,
} as const;

/** Each mark's alpha per theme. Light differs only where the bone ground needs it to keep dark's
 *  contrast; every other alpha is dark's. */
const ALPHAS = {
  dark: DARK_ALPHAS,
  light: {
    ...DARK_ALPHAS,
    unseenFill: 250, // 235 would draw at about 3.0:1 on bone
    tile: 250,
    smoke: 200, // a dark trail on a light ground needs more alpha for the same contrast
    smokeCone: 32, // dark's 1.20:1 in the paler haze
    backLine: 228,
    backLine2: 157,
    backCone: 30,
    wind: 140,
    satTrack: 195,
    satImaging: 113,
    swathEdge: 98,
    passEdge: 152,
    lookLine: 228,
    lookTrack: 213,
    satLabel: 228,
  },
} as const satisfies Record<Theme, Record<string, number>>;

/** CPCB National AQI bands, Good to Severe. */
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

// The edges sit where the data varies: across the season's districts the share runs from about 60%
// (Tarn Taran, where FIRMS sees the most fires) to 100%, and at even quarters every tile fell in the
// top step. The key prints these edges, so they are a drawing choice, never a figure.
export const SCAR_TILE_SHARE_EDGES = [0.7, 0.8, 0.9] as const;
/** Which of the four steps a share falls in. */
export const scarTileStep = (share: number): number => {
  const i = SCAR_TILE_SHARE_EDGES.findIndex((e) => share < e);
  return i === -1 ? SCAR_TILE_SHARE_EDGES.length : i;
};

const FIRE_RADIUS = { minRadiusPx: 2.5, maxRadiusPx: 9 } as const;

/** Dot radius for a fire radiative power: square-root scale, 1 MW at the minimum, 64 MW and up at the maximum. */
export function fireRadiusPx(frpMw: number | null | undefined): number {
  const f = Math.max(1, Math.min(64, frpMw ?? 1));
  return FIRE_RADIUS.minRadiusPx + ((Math.sqrt(f) - 1) / 7) * (FIRE_RADIUS.maxRadiusPx - FIRE_RADIUS.minRadiusPx);
}

/** Half a GK-2A pixel's side: the pixels over Punjab are about 4.5 by 3.6 km (research doc,
 *  geostationary section 3). */
export const GEO_PIXEL_HALF_KM = 2;

/** The scar tiles' sizes (layers/scar-tiles.ts bins and draws with them) and the zooms where the live
 *  map changes view. Their colours are in each theme's SCAR_TILE_STYLE. */
export const SCAR_TILE_GEOMETRY = {
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
} as const;

// ---------------------------------------------------------------------------------------------
// one style object per kind of mark, per theme

function build(theme: Theme) {
  const MARK = MARKS[theme];
  const A = ALPHAS[theme];
  return {
    theme,
    MARK,

    FIRE_STYLE: {
      fill: rgba(MARK.fire, A.fire),
      /** Detections older than six hours stay, dimmer: the map shows when, not only where. */
      fillOld: rgba(MARK.fire, A.fireOld),
      stroke: rgba(MARK.ground, A.fireHalo),
      strokePx: 1,
      ...FIRE_RADIUS,
      recentHours: 6,
    },

    GEO_PIXEL_STYLE: {
      stroke: rgba(MARK.fire, A.geoStroke),
      fill: rgba(MARK.fire, A.geoFill),
      strokePx: 1.5,
    },

    SCAR_STYLE: {
      unseenFill: rgba(MARK.unseen, A.unseenFill),
      unseenStroke: rgba(MARK.unseen, A.unseenStroke),
      alertedFill: rgba(MARK.alerted, A.alertedFill),
      alertedStroke: rgba(MARK.alerted, A.alertedStroke),
      /** Not yet checked against fire alerts: the same muted ink, outline only, so it claims neither. */
      uncheckedFill: rgba(MARK.alerted, A.uncheckedFill),
      uncheckedStroke: rgba(MARK.alerted, A.uncheckedStroke),
      selectedStroke: rgba(MARK.ink, 255),
      strokePx: 1,
      selectedStrokePx: 2,
      /** Dots at the centroid keep sub-pixel fields visible when zoomed out; the polygon covers them up close. */
      dotPx: 2.5,
    },

    SCAR_TILE_STYLE: {
      steps: SCAR_TILE_RAMPS[theme].map((c) => rgba(c, A.tile)),
      stroke: rgba(MARK.ground, 0),
      uncheckedFill: rgba(MARK.alerted, A.uncheckedFill),
      uncheckedStroke: rgba(MARK.alerted, A.uncheckedStroke),
    },

    RISK_STYLE: {
      ramp: RISK_RAMPS[theme].map((c) => rgba(c, 255)),
      fillAlpha: A.riskFill,
      strokePx: 1.5,
      ringPx: 3,
      selectedStroke: rgba(MARK.ink, 255),
    },

    SMOKE_STYLE: {
      color: rgba(MARK.smoke, A.smoke),
      minWidthPx: 1.5,
      maxWidthPx: 4,
      /** The cone between the two forecast models (SYNTHESIS.md: never one path as truth). */
      coneFill: rgba(MARK.smoke, A.smokeCone),
      trailHours: 3,
      loopSeconds: 18,
    },

    /** The air reaching a city: each weather model's centre line (the second model fainter) and the
     *  spread between them, under the back trajectories themselves. */
    BACK_STYLE: {
      line: rgba(MARK.smokeLine, A.backLine),
      line2: rgba(MARK.smokeLine, A.backLine2),
      cone: rgba(MARK.smokeLine, A.backCone),
    },

    WIND_STYLE: {
      color: rgba(MARK.wind, A.wind),
      widthPx: 1,
      loopSeconds: 8,
    },

    AIR_STYLE: {
      ramp: AIR_RAMPS[theme].map((c) => rgba(c, 255)),
      stroke: rgba(MARK.ground, 255),
      radiusPx: 5,
      strokePx: 1.5,
      labelColor: rgba(MARK.ink, A.airLabel),
      labelOutline: rgba(MARK.ground, 255),
      labelPx: 11,
    },

    SATELLITE_STYLE: {
      track: rgba(MARK.satellite, A.satTrack),
      trackImaging: rgba(MARK.satellite, A.satImaging), // Sentinel-2 photographs, it does not count fires
      trackPx: 1.25,
      swathFill: rgba(MARK.satellite, A.swathFill),
      swathEdge: rgba(MARK.satellite, A.swathEdge),
      passFill: rgba(MARK.satellite, A.passFill),
      passEdge: rgba(MARK.satellite, A.passEdge),
      /** The next look at a site: the line its scanner sweeps through the site at closest approach. */
      lookLine: rgba(MARK.satellite, A.lookLine),
      lookLinePx: 1.5,
      /** That pass's ground track, either side of the moment it looks. */
      lookTrack: rgba(MARK.satellite, A.lookTrack),
      lookTrackPx: 1.5,
      dot: rgba(MARK.satellite, 255),
      dotStroke: rgba(MARK.ground, 255),
      dotPx: 4,
      label: rgba(MARK.satellite, A.satLabel),
    },

    BOUNDARY_STYLE: {
      state: MARK.boundaryState,
      district: MARK.boundaryDistrict,
    },

    /** What the page points at (a field, fire or station the list or the investigator highlights, a
     *  named place): an ink outline over a faint ink wash, a label beside it on a ground halo. */
    FOCUS_STYLE: {
      line: rgba(MARK.ink, 255),
      fill: rgba(MARK.ink, A.hlFill),
      label: rgba(MARK.ink, A.place),
      halo: rgba(MARK.ground, 255),
    },

    /** deck.gl autoHighlight: a translucent ink wash, never the library's default blue. */
    HIGHLIGHT: rgba(MARK.ink, A.highlight),
  };
}

export type MapPalette = ReturnType<typeof build>;

const PALETTES: Readonly<Record<Theme, MapPalette>> = { dark: build('dark'), light: build('light') };

/** The marks for a theme (dark when none is given, or for a value that is not a theme). */
export const paletteFor = (theme: Theme | null | undefined = 'dark'): MapPalette => (theme === 'light' ? PALETTES.light : PALETTES.dark);
