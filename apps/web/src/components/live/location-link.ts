/**
 * Location links into mission control: /live?at=<lat>,<lon>[,<zoom>] opens the map centred there.
 * The parser and the link builder sit side by side so the two always agree on the format. Pure
 * functions (no React), so a server page such as a district brief can build its link.
 *
 *   /live?at=30.95,75.35          the map centred near Moga at zoom 9.5
 *   /live?at=30.95,75.35,11       … at zoom 11
 *
 * MapPanel applies the link once, when the map first loads, then takes it off the address.
 */

import { AOI_BBOX } from '@/lib/aoi';

export type LiveLocation = { lat: number; lon: number; zoom: number };

/** The zoom a link without one opens at: a district and its neighbours' edges. */
export const AT_DEFAULT_ZOOM = 9.5;
const ZOOM_MIN = 6;
const ZOOM_MAX = 14;
/** A little past the watched box (Punjab, Haryana, Delhi), so a village on the border still opens. */
const MARGIN = 0.25;

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Inside the watched area: Punjab, Haryana and Delhi with a small margin. */
export function inWatchedArea(lat: number, lon: number): boolean {
  const [w, s, e, n] = AOI_BBOX;
  return lat >= s - MARGIN && lat <= n + MARGIN && lon >= w - MARGIN && lon <= e + MARGIN;
}

/**
 * "30.95,75.35" or "30.95,75.35,11" -> a location; null for anything else (not two or three plain
 * numbers, or a point outside the watched area). A zoom outside 6-14 is brought to the nearest end.
 */
export function parseAt(raw: string | null | undefined): LiveLocation | null {
  if (!raw) return null;
  const parts = raw.split(',').map((x) => x.trim());
  if (parts.length < 2 || parts.length > 3) return null;
  // plain decimals only: Number('') is 0 and Number('1e3') is 1000, neither a coordinate
  if (!parts.every((x) => /^-?\d+(\.\d+)?$/.test(x))) return null;
  const [lat, lon, zoom] = parts.map(Number);
  if (!inWatchedArea(lat, lon)) return null;
  return { lat, lon, zoom: zoom == null ? AT_DEFAULT_ZOOM : clamp(zoom, ZOOM_MIN, ZOOM_MAX) };
}

/** The map the link should fit, in CSS pixels: about mission control's map on a laptop, narrower
 *  than its widest so the points keep a margin, wider than a phone's, where the edges may crop. */
const FIT_W = 420;
const FIT_H = 300;

/**
 * /live?at=… framing a set of points: the middle of their box, at the zoom that fits it (Web
 * Mercator, 256 px tiles), between 7 and 12. Points without coordinates or outside the watched
 * area are left out; with none left, plain /live.
 */
export function liveAtHref(points: readonly { lat?: number | null; lon?: number | null }[]): string {
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  for (const p of points) {
    if (p.lat == null || p.lon == null || !Number.isFinite(p.lat) || !Number.isFinite(p.lon) || !inWatchedArea(p.lat, p.lon)) continue;
    w = Math.min(w, p.lon);
    e = Math.max(e, p.lon);
    s = Math.min(s, p.lat);
    n = Math.max(n, p.lat);
  }
  if (!Number.isFinite(w)) return '/live';
  const lat = (s + n) / 2;
  const lon = (w + e) / 2;
  // a lone village still opens on its surroundings: never narrower than ~2 km
  const spanLon = Math.max(e - w, 0.02);
  const spanLat = Math.max(n - s, 0.02);
  const zx = Math.log2((FIT_W * 360) / (256 * spanLon));
  const zy = Math.log2((FIT_H * 360 * Math.cos((lat * Math.PI) / 180)) / (256 * spanLat));
  const zoom = Math.floor(clamp(Math.min(zx, zy), 7, 12) * 10) / 10;
  return `/live?at=${lat.toFixed(4)},${lon.toFixed(4)},${zoom}`;
}
