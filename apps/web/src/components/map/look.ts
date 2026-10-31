/**
 * The next look at a site, as geometry and words, from the orbital elements alone (no deck.gl here,
 * so the screen's key can read it before the map's chunk loads; layers/satellites.ts draws it).
 */

import { number, timeIST } from '@/lib/format';
import { bearing, destination, groundTrack, havKm, subpoint, type LatLon, type PassWindow, type Satellite } from '@/lib/orbits';

const COMPASS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];

/** Split a path wherever it jumps across the antimeridian, so a PathLayer never draws the long way round. */
function splitAtJumps(path: [number, number][]): [number, number][][] {
  const out: [number, number][][] = [];
  let cur: [number, number][] = [];
  for (const p of path) {
    if (cur.length && Math.abs(p[0] - cur[cur.length - 1][0]) > 180) {
      if (cur.length > 1) out.push(cur);
      cur = [];
    }
    cur.push(p);
  }
  if (cur.length > 1) out.push(cur);
  return out;
}

export type NextLook = {
  /** The scan line through the site at closest approach, across the whole swath. */
  scan: [number, number][][];
  /** The pass's ground track, 8 minutes either side. */
  track: [number, number][][];
  overhead: [number, number];
  site: [number, number];
  /** "Suomi NPP 13:01 IST", for the map, placed on the scan line between the site and the track
   *  (up to 200 km out), so it names the line without sitting on the burn belt's own marks. */
  label: string;
  labelAt: [number, number];
  /** The line's direction on a north-up map at `labelAt`, degrees counter-clockwise from east,
   *  kept between -90 and 90 so the label reads left to right (Web Mercator keeps angles). */
  labelAngle: number;
  /** Ground distance from the site to the track at that moment, and which way the track lies. */
  offKm: number;
  toward: string | null;
};

/** The label's direction along the line at `p` (toward the ground point `a`), as `labelAngle`. */
function angleAt(p: LatLon, a: LatLon): number {
  const b = bearing(p, a);
  let angle = (Math.atan2(Math.cos(b), Math.sin(b)) * 180) / Math.PI;
  if (angle > 90) angle -= 180;
  if (angle < -90) angle += 180;
  return angle;
}

/**
 * Where to write the next look's label on the map now: the first point along the scan line, out
 * from the site toward the track (up to 400 km) and then the other way, where the whole label lies
 * well inside the view and clear of every mark in `avoid` (the fires and scars drawn) by `clearPx`,
 * so it names the line without sitting on the burn belt; failing that the first such point in view,
 * else the default. `kmPerPx` is the ground scale at the current zoom.
 */
export function placeLookLabel(
  l: NextLook,
  view: readonly [number, number, number, number] | null,
  avoid: readonly (readonly [number, number])[],
  kmPerPx: number,
  clearPx = 10,
): { at: [number, number]; angle: number } {
  const fallback = { at: l.labelAt, angle: l.labelAngle };
  if (!view || l.offKm <= 1 || !(kmPerPx > 0)) return fallback;
  const site: LatLon = { lon: l.site[0], lat: l.site[1] };
  const a: LatLon = { lon: l.overhead[0], lat: l.overhead[1] };
  const b = bearing(site, a);
  const [w, s, e, n] = view;
  const ix = (e - w) * 0.1;
  const iy = (n - s) * 0.1;
  const inView = (p: LatLon) => p.lon > w + ix && p.lon < e - ix && p.lat > s + iy && p.lat < n - iy;
  // half the label's length (12 px type, ~6.6 px a character) and the gap it keeps, in km
  const half = l.label.length * 3.3 * kmPerPx;
  const clearKm = clearPx * kmPerPx;
  const dLat = clearKm / 111;
  const clear = (p: LatLon) => {
    const dLon = dLat / Math.max(0.2, Math.cos((p.lat * Math.PI) / 180));
    for (const [x, y] of avoid) {
      if (Math.abs(y - p.lat) > dLat || Math.abs(x - p.lon) > dLon) continue;
      if (havKm(p, { lon: x, lat: y }) < clearKm) return false;
    }
    return true;
  };
  const step = Math.max(5, clearKm);
  let firstInView: LatLon | null = null;
  for (const dir of [b, b + Math.PI]) {
    const far = dir === b ? Math.min(400, l.offKm - half) : 400;
    for (let km = half + clearKm; km <= far; km += step) {
      const ends = [destination(site, dir, km - half), destination(site, dir, km + half)];
      if (!ends.every(inView)) continue;
      const p = destination(site, dir, km);
      firstInView ??= p;
      let ok = true;
      for (let t = km - half; t <= km + half + 1e-6 && ok; t += step) ok = clear(destination(site, dir, t));
      if (ok) return { at: [p.lon, p.lat], angle: angleAt(p, a) };
    }
  }
  return firstInView ? { at: [firstInView.lon, firstInView.lat], angle: angleAt(firstInView, a) } : fallback;
}

/**
 * The geometry of the next look at `site`, from the orbital elements alone. At closest approach the
 * site lies on the perpendicular from the ground track (that is what closest approach means), and a
 * cross-track scanner (VIIRS, MODIS) images that perpendicular in the same sweep; so the line through
 * the satellite's ground point and the site, run out to the swath edges, is where it looks at that
 * moment. Null when SGP4 cannot place the satellite.
 */
export function nextLookParts(sats: readonly Satellite[], pass: PassWindow, site: LatLon): NextLook | null {
  const sat = sats.find((x) => x.key === pass.satKey);
  if (!sat) return null;
  const a = subpoint(sat, pass.tca);
  if (!a) return null;
  const offKm = havKm(a, site);
  // the scan line: from the far swath edge, through the ground point, to the near edge past the site
  const ahead = subpoint(sat, new Date(pass.tca.getTime() + 1000));
  const toSite = offKm > 1 || !ahead ? bearing(a, site) : bearing(a, ahead) + Math.PI / 2;
  const half = sat.halfSwathKm;
  const scan: [number, number][] = [];
  for (let k = -half; k <= half + 1e-6; k += Math.max(10, half / 60)) {
    const p = destination(a, k >= 0 ? toSite : toSite + Math.PI, Math.abs(k));
    scan.push([p.lon, p.lat]);
  }
  const track = groundTrack(sat, pass.tca, { beforeMin: 8, afterMin: 8, stepS: 15 });
  const toTrack = offKm > 1 ? COMPASS[Math.round((((bearing(site, a) * 180) / Math.PI + 360) % 360) / 45) % 8] : null;
  const label = `${pass.satName} ${timeIST(pass.tca)} IST`;
  // on screen the line runs along (sin b, -cos b); it rises to the right when sin b * cos b > 0
  const b = offKm > 1 ? bearing(site, a) : toSite;
  const at = offKm > 1 ? destination(site, b, Math.min(offKm * 0.5, 200)) : site;
  const bAt = offKm > 1 ? bearing(at, a) : b;
  // screen direction of a bearing: (sin b, cos b) with y up; its angle from east, folded to +-90
  let angle = (Math.atan2(Math.cos(bAt), Math.sin(bAt)) * 180) / Math.PI;
  if (angle > 90) angle -= 180;
  if (angle < -90) angle += 180;
  return { scan: splitAtJumps(scan), track, overhead: [a.lon, a.lat], site: [site.lon, site.lat], label, labelAt: [at.lon, at.lat], labelAngle: angle, offKm, toward: toTrack };
}

/** "its track 1,034 km to the east", or "from straight overhead". */
export const lookOffsetText = (l: Pick<NextLook, 'offKm' | 'toward'>): string =>
  l.toward ? `its track ${number(l.offKm)} km to the ${l.toward}` : 'from straight overhead';
