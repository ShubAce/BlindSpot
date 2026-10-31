import 'server-only';

import { cacheLife } from 'next/cache';

import { AOI_BBOX } from '@/lib/aoi';

/**
 * NASA GIBS (Global Imagery Browse Services): keyless satellite pictures of the plains for the latest
 * day that has them. An official open service, used directly by the browser as <img> sources.
 *
 * - The latest day comes from the layer's own time domain (WMTS DescribeDomains), never from a date
 *   typed here. A daily layer lists today's UTC date as soon as its first granule anywhere is in, which
 *   is hours before the swath over Punjab is processed, so a day is only used once the hour Punjab's
 *   pass is normally ready has gone by (`readyHourUtc`, below); until then the day before is used.
 * - Pictures are requested from the Web Mercator WMS so their proportions are true on screen, at the
 *   exact pixel size they are shown at (no browser upscaling).
 * - Never throws: a failed domain read returns null and the page says the picture is unavailable.
 */

export type GibsLayer = {
  /** GIBS layer identifiers, drawn bottom to top. The first one decides the day. */
  layers: string[];
  /** The tile matrix set its DescribeDomains lives under (250m for corrected reflectance). */
  matrix: '250m' | '500m';
  /** The UTC hour after which today's swath over Punjab is normally processed (pass + ~3 h). */
  readyHourUtc: number;
  /** For captions: the sensor and satellite in words. */
  sensor: string;
};

/** The pictures the landing uses. Pass times are the published orbits of each satellite (local
 *  overpass ~10:30 Terra, ~13:30 VIIRS and Aqua, ~01:30 VIIRS at night), plus the NRT latency. */
export const GIBS = {
  viirsTrueColour: { layers: ['VIIRS_NOAA20_CorrectedReflectance_TrueColor'], matrix: '250m', readyHourUtc: 12, sensor: 'VIIRS on NOAA-20, true colour' },
  viirsFires: {
    layers: ['VIIRS_SNPP_CorrectedReflectance_TrueColor', 'VIIRS_SNPP_Thermal_Anomalies_375m_Day'],
    matrix: '250m',
    readyHourUtc: 12,
    sensor: 'VIIRS on Suomi NPP, true colour with its fire detections in red',
  },
  modisFires: {
    layers: ['MODIS_Terra_CorrectedReflectance_TrueColor', 'VIIRS_NOAA20_Thermal_Anomalies_375m_All'],
    matrix: '250m',
    readyHourUtc: 9,
    sensor: 'MODIS on Terra, true colour, with VIIRS fire detections in red',
  },
  nightBand: { layers: ['VIIRS_NOAA20_DayNightBand'], matrix: '500m', readyHourUtc: 23, sensor: 'VIIRS Day/Night Band on NOAA-20' },
} as const satisfies Record<string, GibsLayer>;

export type GibsKey = keyof typeof GIBS;

const WMTS = 'https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/1.0.0';
const WMS = 'https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi';

/** The last date of a DescribeDomains time domain: "...,2022-01-14/2026-10-30/P1D" -> "2026-10-30". */
export function lastDomainDay(xml: string): string | null {
  const m = xml.match(/<Domain>([^<]+)<\/Domain>/);
  if (!m) return null;
  const parts = m[1].split(',').map((s) => s.trim()).filter(Boolean);
  const last = parts[parts.length - 1];
  if (!last) return null;
  const pieces = last.split('/');
  const day = pieces.length >= 2 ? pieces[1] : pieces[0];
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null;
}

function previousDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

/** The newest day a layer has a usable picture of Punjab for, or null if GIBS did not answer. */
export async function gibsDay(key: GibsKey): Promise<string | null> {
  'use cache';
  const layer: GibsLayer = GIBS[key];
  try {
    const res = await fetch(`${WMTS}/${layer.layers[0]}/default/${layer.matrix}/all/all.xml`, { signal: AbortSignal.timeout(6_000) });
    if (!res.ok) throw new Error(String(res.status));
    const last = lastDomainDay(await res.text());
    if (!last) throw new Error('no domain');
    const now = new Date();
    const today = now.toISOString().slice(0, 10);
    const day = last >= today && now.getUTCHours() < layer.readyHourUtc ? previousDay(today) : last > today ? today : last;
    // Short lifetimes keep this out of the build-time prerender (as lib/api-server.ts does).
    cacheLife({ stale: 60, revalidate: 120, expire: 240 });
    return day;
  } catch {
    cacheLife({ stale: 30, revalidate: 5, expire: 60 });
    return null;
  }
}

// ---------------------------------------------------------------------------------------------
// picture URLs

/** The middle of the area BlindSpot watches (lib/aoi.ts), where the plains pictures are centred. */
export const AOI_CENTRE: [number, number] = [(AOI_BBOX[0] + AOI_BBOX[2]) / 2, (AOI_BBOX[1] + AOI_BBOX[3]) / 2];

const R = 6378137;
const mercX = (lon: number) => (R * lon * Math.PI) / 180;
const mercY = (lat: number) => R * Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));

/**
 * A WMS GetMap URL for `[minLon, minLat, maxLon, maxLat]` at `width` px wide; the height follows the
 * box so nothing is stretched. Returns the URL and the pixel size to set on the <img>.
 */
export function gibsPicture(key: GibsKey, day: string, bbox: readonly [number, number, number, number], width: number) {
  const layer: GibsLayer = GIBS[key];
  const [x0, y0, x1, y1] = [mercX(bbox[0]), mercY(bbox[1]), mercX(bbox[2]), mercY(bbox[3])];
  const height = Math.round((width * (y1 - y0)) / (x1 - x0));
  const q = new URLSearchParams({
    SERVICE: 'WMS',
    REQUEST: 'GetMap',
    VERSION: '1.3.0',
    CRS: 'EPSG:3857',
    FORMAT: 'image/jpeg',
    TIME: day,
    LAYERS: layer.layers.join(','),
    BBOX: [x0, y0, x1, y1].map((v) => v.toFixed(0)).join(','),
    WIDTH: String(width),
    HEIGHT: String(height),
  });
  return { src: `${WMS}?${q.toString()}`, width, height, sensor: layer.sensor, day };
}

/** A box of `widthDeg` by a height chosen so the picture has the ratio w:h on screen, centred on
 *  [lon, lat]. Used to frame portrait (4:5) and wide (16:9) crops of the plains. */
export function boxAround(lon: number, lat: number, widthDeg: number, ratioWOverH: number): [number, number, number, number] {
  const halfW = widthDeg / 2;
  const x0 = mercX(lon - halfW);
  const x1 = mercX(lon + halfW);
  const hM = (x1 - x0) / ratioWOverH;
  const yc = mercY(lat);
  const toLat = (y: number) => ((2 * Math.atan(Math.exp(y / R)) - Math.PI / 2) * 180) / Math.PI;
  return [lon - halfW, toLat(yc - hM / 2), lon + halfW, toLat(yc + hM / 2)];
}
