/**
 * The area BlindSpot watches, as configuration (not data).
 *
 * These mirror services/api/blindspot/config.py (`aoi_bbox`, `met_bbox`) so the map frames the same
 * box the backend clips to. Nothing here is a measurement: they are where we look, not what we saw.
 */

/** [minLon, minLat, maxLon, maxLat], WGS84 degrees. */
export type BBox = readonly [number, number, number, number];

/** Punjab + Haryana + Delhi NCR (services/api config `aoi_bbox`). Counts are clipped to state
 *  polygons on the server; this box only frames the map. */
export const AOI_BBOX: BBox = [73.8, 27.6, 77.7, 32.6];

/** The wider box the wind grid covers (config `met_bbox`), so smoke leaving the AOI stays drawn. */
export const MET_BBOX: BBox = [70.0, 26.0, 80.0, 34.0];

/** The map may not pan past North India (lon 68-82, lat 24-36): the national outline is only
 *  ever drawn from our Survey of India boundaries, never from a basemap. */
export const MAP_MAX_BOUNDS: [number, number, number, number] = [68, 24, 82, 36];

/** A point for "who is watching Punjab now": Sangrur district HQ, the research test site
 *  (docs/research/firms-and-orbits.md section 3.3). A VIIRS or MODIS swath that covers it covers
 *  most of the state's burning belt. /api/orbits/passes takes any lat/lon; this is a default. */
export const REFERENCE_SITE = { name: 'Sangrur', lat: 30.25, lon: 75.84 } as const;

export type Site = { lat: number; lon: number; name?: string };

/** "minLon,minLat,maxLon,maxLat" for a `bbox=` query parameter. */
export const bboxParam = (b: BBox): string => b.map((v) => +v.toFixed(5)).join(',');
