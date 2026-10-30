/**
 * Satellites in the browser: where each fire satellite is, what it can see, and when it next looks
 * at a place. All of it is computed from the CelesTrak elements the API serves (/api/orbits/tle),
 * the same rows the backend's pass predictions start from, so the map and /api/orbits/passes agree.
 *
 * The visibility criterion is the backend's (docs/research/firms-and-orbits.md section 3.2): a
 * cross-track scanner images a site when the site lies within half a swath of the ground track at
 * the time of closest approach (TCA). VIIRS 1520 km, MODIS 1165 km, Sentinel-2 MSI 148 km (145 km
 * nominal plus 3 km tolerance; MSI counts only in daylight). A pass's window is the stretch of time
 * the site stays within that distance of the sub-satellite point (equivalently, the satellite stands
 * above the elevation of the swath edge). TCA matches skyfield to about a second (research doc 5.2);
 * the window edges can differ from the API's by up to a minute if the backend pads its elevation mask.
 *
 * Self-check: src/lib/orbits.check.ts compares these functions with /api/orbits/passes.
 *
 * Only `import type` from other app modules here, so Node can run this file directly for the check.
 */

import type { OrbitTle, OrbitsTleResponse } from './api';
import { degreesLat, degreesLong, eciToGeodetic, gstime, propagate, twoline2satrec, type SatRec } from './sgp4';

export const R_EARTH_KM = 6371.0088;
const D2R = Math.PI / 180;
const MIN_MS = 60_000;

/** Half-swath per instrument, the criterion the backend uses (research doc 3.2 and SYNTHESIS.md). */
export const HALF_SWATH_KM: Readonly<Record<string, number>> = { VIIRS: 1520, MODIS: 1165, MSI: 148 };
/** The instruments FIRMS counts fires with. Sentinel-2 (MSI) photographs scars; it does not count fires. */
export const FIRE_INSTRUMENTS: readonly string[] = ['VIIRS', 'MODIS'];

export type LatLon = { lat: number; lon: number };
export type Ring = [number, number][]; // [lon, lat], closed

export type Satellite = {
  key: string;
  name: string;
  noradId: number;
  instrument: string;
  halfSwathKm: number;
  epoch: Date | null;
  satrec: SatRec;
};

export type PassWindow = {
  satKey: string;
  satName: string;
  instrument: string;
  /** The site enters the swath. */
  start: Date;
  /** Time of closest approach: the moment the scanner images the site. */
  tca: Date;
  /** The site leaves the swath. */
  end: Date;
  /** Ground distance from the site to the track at TCA. */
  crossTrackKm: number;
  /** Elevation of the satellite seen from the site at TCA. */
  maxElevationDeg: number;
};

// ---------------------------------------------------------------------------------------------
// elements -> satellites

/** The rows of /api/orbits/tle, from the response or a bare array. */
export function tleRows(input: OrbitsTleResponse | readonly OrbitTle[] | null | undefined): readonly OrbitTle[] {
  if (!input) return [];
  if (Array.isArray(input)) return input as readonly OrbitTle[];
  return (input as OrbitsTleResponse).satellites ?? [];
}

/** The half-swath to use for one satellite: the API's own value if it states one, else the
 *  research criterion for its instrument, else half the stated swath width. */
export function halfSwathOf(t: Pick<OrbitTle, 'instrument' | 'swath_km' | 'half_swath_km'>): number {
  if (t.half_swath_km && t.half_swath_km > 0) return t.half_swath_km;
  const byInstrument = HALF_SWATH_KM[(t.instrument ?? '').toUpperCase()];
  if (byInstrument) return byInstrument;
  return t.swath_km && t.swath_km > 0 ? t.swath_km / 2 : 0;
}

/** Parse the API's TLE rows into propagatable satellites. Malformed rows are skipped, not guessed. */
export function parseTles(input: OrbitsTleResponse | readonly OrbitTle[] | null | undefined): Satellite[] {
  const rows = tleRows(input);
  const out: Satellite[] = [];
  for (const t of rows) {
    if (!t?.line1 || !t?.line2) continue;
    let satrec: SatRec;
    try {
      satrec = twoline2satrec(t.line1.trim(), t.line2.trim());
    } catch {
      continue;
    }
    if (satrec.error) continue;
    const half = halfSwathOf(t);
    if (!(half > 0)) continue;
    const epoch = t.epoch ? new Date(t.epoch) : null;
    out.push({
      key: t.key,
      name: t.name,
      noradId: t.norad_id,
      instrument: (t.instrument ?? '').toUpperCase(),
      halfSwathKm: half,
      epoch: epoch && Number.isFinite(epoch.getTime()) ? epoch : null,
      satrec,
    });
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// geometry on a sphere

/** Sub-satellite point (and height) at a moment, or null if SGP4 cannot propagate it. */
export function subpoint(sat: Satellite, date: Date): (LatLon & { heightKm: number }) | null {
  const pv = propagate(sat.satrec, date);
  if (!pv || !pv.position || typeof pv.position !== 'object') return null;
  const g = eciToGeodetic(pv.position, gstime(date));
  const lat = degreesLat(g.latitude);
  const lon = degreesLong(g.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  return { lat, lon, heightKm: g.height };
}

/** Great-circle distance in km. */
export function havKm(a: LatLon, b: LatLon): number {
  const p1 = a.lat * D2R;
  const p2 = b.lat * D2R;
  const dl = (b.lon - a.lon) * D2R;
  const x = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R_EARTH_KM * Math.asin(Math.min(1, Math.sqrt(x)));
}

/** Initial bearing from a to b, radians clockwise from north. */
export function bearing(a: LatLon, b: LatLon): number {
  const p1 = a.lat * D2R;
  const p2 = b.lat * D2R;
  const dl = (b.lon - a.lon) * D2R;
  return Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl));
}

/** The point `km` along a great circle from `a` at bearing `brg` (radians). */
export function destination(a: LatLon, brg: number, km: number): LatLon {
  const d = km / R_EARTH_KM;
  const p1 = a.lat * D2R;
  const l1 = a.lon * D2R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(brg));
  const l2 = l1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return { lat: p2 / D2R, lon: ((((l2 / D2R + 540) % 360) + 360) % 360) - 180 };
}

/** Elevation of a satellite at height h seen from a site `groundKm` from its sub-point. */
export function elevationDeg(groundKm: number, heightKm: number): number {
  const lam = groundKm / R_EARTH_KM;
  return Math.atan2(Math.cos(lam) - R_EARTH_KM / (R_EARTH_KM + heightKm), Math.sin(lam)) / D2R;
}

/** Sun elevation in degrees (low-precision solar position, about 0.1 degree): MSI only images in daylight. */
export function sunElevationDeg(date: Date, site: LatLon): number {
  const d = (date.getTime() - 946_728_000_000) / 86_400_000; // days since J2000.0
  const g = (357.529 + 0.98560028 * d) * D2R;
  const q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * D2R;
  const e = (23.439 - 0.00000036 * d) * D2R;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L));
  const dec = Math.asin(Math.sin(e) * Math.sin(L));
  const gmstDeg = ((18.697374558 + 24.06570982441908 * d) % 24) * 15;
  const ha = (gmstDeg + site.lon) * D2R - ra;
  const lat = site.lat * D2R;
  return Math.asin(Math.sin(lat) * Math.sin(dec) + Math.cos(lat) * Math.cos(dec) * Math.cos(ha)) / D2R;
}

// ---------------------------------------------------------------------------------------------
// tracks and swaths, safe across the antimeridian

/** Unwrap longitudes so consecutive points never jump by more than 180 degrees. */
function unwrap(points: [number, number][]): [number, number][] {
  const out: [number, number][] = [];
  for (const [lon, lat] of points) {
    if (!out.length) {
      out.push([lon, lat]);
      continue;
    }
    const prev = out[out.length - 1][0];
    let x = lon;
    while (x - prev > 180) x -= 360;
    while (x - prev < -180) x += 360;
    out.push([x, lat]);
  }
  return out;
}

/** Split an unwrapped line into pieces inside [-180, 180], interpolating the crossing points. */
function splitLine(unwrapped: [number, number][]): [number, number][][] {
  const pieces: [number, number][][] = [];
  for (const shift of [-360, 0, 360]) {
    let cur: [number, number][] = [];
    for (let i = 0; i < unwrapped.length; i++) {
      const p: [number, number] = [unwrapped[i][0] + shift, unwrapped[i][1]];
      const inside = p[0] >= -180 && p[0] <= 180;
      if (i > 0) {
        const q: [number, number] = [unwrapped[i - 1][0] + shift, unwrapped[i - 1][1]];
        const qIn = q[0] >= -180 && q[0] <= 180;
        if (inside !== qIn) {
          const edge = (inside ? q[0] : p[0]) > 180 ? 180 : -180;
          const t = (edge - q[0]) / (p[0] - q[0]);
          const cross: [number, number] = [edge, q[1] + t * (p[1] - q[1])];
          if (inside) cur = [cross];
          else {
            cur.push(cross);
            if (cur.length > 1) pieces.push(cur);
            cur = [];
          }
        }
      }
      if (inside) cur.push(p);
    }
    if (cur.length > 1) pieces.push(cur);
  }
  return pieces;
}

/** Clip a ring to lon <= max or lon >= min (one Sutherland-Hodgman pass). */
function clipRing(ring: [number, number][], edge: number, keepBelow: boolean): [number, number][] {
  const out: [number, number][] = [];
  const inside = (p: [number, number]) => (keepBelow ? p[0] <= edge : p[0] >= edge);
  for (let i = 0; i < ring.length; i++) {
    const cur = ring[i];
    const prev = ring[(i + ring.length - 1) % ring.length];
    const cIn = inside(cur);
    const pIn = inside(prev);
    if (cIn !== pIn) {
      const t = (edge - prev[0]) / (cur[0] - prev[0]);
      out.push([edge, prev[1] + t * (cur[1] - prev[1])]);
    }
    if (cIn) out.push(cur);
  }
  return out;
}

/** Split an unwrapped polygon ring into closed rings that each lie inside [-180, 180]. */
function splitRing(unwrappedOpen: [number, number][]): Ring[] {
  const out: Ring[] = [];
  for (const shift of [-360, 0, 360]) {
    let r = unwrappedOpen.map(([x, y]) => [x + shift, y] as [number, number]);
    if (r.every(([x]) => x < -180) || r.every(([x]) => x > 180)) continue;
    r = clipRing(clipRing(r, 180, true), -180, false);
    if (r.length >= 3) out.push([...r, r[0]] as Ring);
  }
  return out;
}

export type TrackPoint = { lon: number; lat: number; t: Date };

/** Sub-satellite points every `stepS` seconds from `from` to `to`. */
export function trackPoints(sat: Satellite, from: Date, to: Date, stepS = 30): TrackPoint[] {
  const out: TrackPoint[] = [];
  for (let t = from.getTime(); t <= to.getTime(); t += stepS * 1000) {
    const p = subpoint(sat, new Date(t));
    if (p) out.push({ lon: p.lon, lat: p.lat, t: new Date(t) });
  }
  return out;
}

/** Ground track around `center` (default plus and minus 50 minutes), as line pieces that never
 *  cross the antimeridian (each [lon, lat][] is safe for a PathLayer). */
export function groundTrack(sat: Satellite, center: Date, opts: { beforeMin?: number; afterMin?: number; stepS?: number } = {}): [number, number][][] {
  const pts = trackPoints(sat, new Date(center.getTime() - (opts.beforeMin ?? 50) * MIN_MS), new Date(center.getTime() + (opts.afterMin ?? 50) * MIN_MS), opts.stepS ?? 30);
  return splitLine(unwrap(pts.map((p) => [p.lon, p.lat])));
}

/**
 * The ground the swath sweeps between `from` and `to`, as closed rings (lon, lat) inside
 * [-180, 180]. Left edge forward, right edge back, built from great-circle offsets perpendicular to
 * the track. Long sweeps are cut into chunks of about 8 minutes, and points poleward of
 * `maxAbsLat` are left out, so every ring is simple enough to triangulate (a 3,040 km VIIRS swath
 * folds over itself near the pole).
 */
export function swathPolygons(sat: Satellite, from: Date, to: Date, opts: { stepS?: number; halfKm?: number; maxAbsLat?: number; chunkMin?: number } = {}): Ring[] {
  const stepS = opts.stepS ?? 20;
  const half = opts.halfKm ?? sat.halfSwathKm;
  const maxAbsLat = opts.maxAbsLat ?? 65;
  const chunk = Math.max(2, Math.round(((opts.chunkMin ?? 8) * 60) / stepS));
  type Edge = { left: LatLon; right: LatLon };
  const runs: Edge[][] = [];
  let run: Edge[] = [];
  for (let t = from.getTime(); t <= to.getTime(); t += stepS * 1000) {
    const a = subpoint(sat, new Date(t));
    const b = subpoint(sat, new Date(t + 1000));
    if (!a || !b || Math.abs(a.lat) > maxAbsLat) {
      if (run.length > 1) runs.push(run);
      run = [];
      continue;
    }
    const brg = bearing(a, b);
    run.push({ left: destination(a, brg - Math.PI / 2, half), right: destination(a, brg + Math.PI / 2, half) });
  }
  if (run.length > 1) runs.push(run);

  const rings: Ring[] = [];
  for (const r of runs) {
    for (let i = 0; i < r.length - 1; i += chunk) {
      const part = r.slice(i, Math.min(r.length, i + chunk + 1));
      if (part.length < 2) continue;
      const open: [number, number][] = [
        ...part.map((e) => [e.left.lon, e.left.lat] as [number, number]),
        ...part.slice().reverse().map((e) => [e.right.lon, e.right.lat] as [number, number]),
      ];
      rings.push(...splitRing(unwrap(open)));
    }
  }
  return rings;
}

/** The strip a pass imaged around its TCA (default TCA plus and minus 6 minutes). */
export function swathForPass(sat: Satellite, pass: Pick<PassWindow, 'tca'>, padMin = 6): Ring[] {
  return swathPolygons(sat, new Date(pass.tca.getTime() - padMin * MIN_MS), new Date(pass.tca.getTime() + padMin * MIN_MS));
}

// ---------------------------------------------------------------------------------------------
// passes over a site

/**
 * Every pass in which `site` lies inside the swath, from `from` for `hours`, including one already
 * in progress at `from`. 30 s scan for minima of the ground distance, TCA refined to 0.5 s, window
 * edges by bisection to 0.5 s. MSI passes count only when the sun is up at the site.
 */
export function passesOver(site: LatLon, sat: Satellite, from: Date, hours: number): PassWindow[] {
  const half = sat.halfSwathKm;
  const dist = (t: number) => {
    const p = subpoint(sat, new Date(t));
    return p ? havKm(site, p) : Infinity;
  };
  const step = 30_000;
  const t0 = from.getTime() - 20 * MIN_MS; // catch a pass already under way
  const tEnd = from.getTime() + hours * 3600_000;
  const out: PassWindow[] = [];
  let d0 = dist(t0);
  let d1 = dist(t0 + step);
  for (let t = t0 + step; t < tEnd; t += step) {
    const d2 = dist(t + step);
    if (d1 <= d0 && d1 < d2 && d1 < half + 300) {
      let lo = t - step;
      let hi = t + step;
      while (hi - lo > 500) {
        const m1 = lo + (hi - lo) / 3;
        const m2 = hi - (hi - lo) / 3;
        if (dist(m1) < dist(m2)) hi = m2;
        else lo = m1;
      }
      const tca = (lo + hi) / 2;
      const dmin = dist(tca);
      if (dmin <= half) {
        const edge = (a: number, b: number) => {
          // a is outside the swath, b inside; find the crossing.
          while (Math.abs(b - a) > 500) {
            const m = (a + b) / 2;
            if (dist(m) <= half) b = m;
            else a = m;
          }
          return (a + b) / 2;
        };
        const start = edge(tca - 20 * MIN_MS, tca);
        const end = edge(tca + 20 * MIN_MS, tca);
        const sp = subpoint(sat, new Date(tca));
        const daylightOk = sat.instrument !== 'MSI' || sunElevationDeg(new Date(tca), site) > 0;
        if (end >= from.getTime() && daylightOk) {
          out.push({
            satKey: sat.key,
            satName: sat.name,
            instrument: sat.instrument,
            start: new Date(start),
            tca: new Date(tca),
            end: new Date(end),
            crossTrackKm: dmin,
            maxElevationDeg: sp ? elevationDeg(dmin, sp.heightKm) : NaN,
          });
        }
      }
    }
    d0 = d1;
    d1 = d2;
  }
  return out;
}

const onlyInstruments = (sats: readonly Satellite[], instruments?: readonly string[]) =>
  instruments ? sats.filter((s) => instruments.includes(s.instrument)) : sats.slice();

/** All passes of `sats` over `site`, sorted by TCA. */
export function predictPasses(site: LatLon, sats: readonly Satellite[], from: Date, hours: number, instruments?: readonly string[]): PassWindow[] {
  return onlyInstruments(sats, instruments)
    .flatMap((s) => passesOver(site, s, from, hours))
    .sort((a, b) => a.tca.getTime() - b.tca.getTime());
}

/** Satellites whose swath covers `site` at `now`, with the moment each one leaves. */
export function isWatching(site: LatLon, sats: readonly Satellite[], now: Date, instruments: readonly string[] = FIRE_INSTRUMENTS): { sat: Satellite; pass: PassWindow; until: Date }[] {
  const out: { sat: Satellite; pass: PassWindow; until: Date }[] = [];
  for (const sat of onlyInstruments(sats, instruments)) {
    for (const p of passesOver(site, sat, new Date(now.getTime() - 25 * MIN_MS), 1)) {
      if (p.start <= now && now <= p.end) out.push({ sat, pass: p, until: p.end });
    }
  }
  return out.sort((a, b) => a.until.getTime() - b.until.getTime());
}

/** The first pass to begin after `from` (a pass under way at `from` is "watching", not "next"). */
export function nextPass(site: LatLon, sats: readonly Satellite[], from: Date, opts: { hoursAhead?: number; instruments?: readonly string[] } = {}): PassWindow | null {
  const all = predictPasses(site, sats, from, opts.hoursAhead ?? 48, opts.instruments ?? FIRE_INSTRUMENTS);
  return all.find((p) => p.start.getTime() > from.getTime()) ?? null;
}

/** The last pass to have ended before `at`. */
export function lastPass(site: LatLon, sats: readonly Satellite[], at: Date, opts: { hoursBack?: number; instruments?: readonly string[] } = {}): PassWindow | null {
  const hours = opts.hoursBack ?? 24;
  const all = predictPasses(site, sats, new Date(at.getTime() - hours * 3600_000), hours, opts.instruments ?? FIRE_INSTRUMENTS);
  return all.filter((p) => p.end.getTime() < at.getTime()).pop() ?? null;
}

// ---------------------------------------------------------------------------------------------
// the landing page's live sentence

export type LiveStatus = {
  /** Fire satellites whose swath covers the site now, soonest to leave first. */
  watching: { name: string; key: string; instrument: string; until: Date }[];
  /** The next fire satellite to look, at its time of closest approach. */
  next: { name: string; key: string; instrument: string; at: Date; start: Date; end: Date } | null;
  /** The most recent finished look. */
  last: { name: string; key: string; instrument: string; at: Date; end: Date } | null;
};

// Predictions are cached per site, element set and 10-minute bucket: liveStatus can be called every
// second (a countdown) for the cost of a lookup.
const statusCache = new Map<string, { passes: PassWindow[]; from: number; to: number }>();
const satCache = new Map<string, Satellite[]>();

function tleSignature(rows: readonly OrbitTle[]): string {
  return rows.map((r) => `${r.norad_id}:${r.line1}`).join('|');
}

/**
 * "No fire satellite can see Punjab right now. NOAA-21 passes at 13:12 IST, in 9 h 41 min."
 * Pure in its inputs: the same elements and the same `now` give the same answer.
 */
export function liveStatus(site: LatLon, tles: OrbitsTleResponse | readonly OrbitTle[] | null | undefined, now: Date, instruments: readonly string[] = FIRE_INSTRUMENTS): LiveStatus {
  const rows = tleRows(tles);
  const sig = tleSignature(rows);
  let sats = satCache.get(sig);
  if (!sats) {
    sats = parseTles(rows);
    if (satCache.size > 4) satCache.clear();
    satCache.set(sig, sats);
  }
  const bucket = Math.floor(now.getTime() / (10 * MIN_MS)) * 10 * MIN_MS;
  const key = `${site.lat.toFixed(3)},${site.lon.toFixed(3)}|${instruments.join(',')}|${bucket}|${sig}`;
  let cached = statusCache.get(key);
  if (!cached) {
    // Fire satellites look at Punjab at least every ~5 h (research doc 3.4), so 12 h back always
    // holds the last look; 49 h ahead holds the next one with a day to spare.
    const from = bucket - 12 * 3600_000;
    const to = bucket + 49 * 3600_000;
    cached = { passes: predictPasses(site, sats, new Date(from), (to - from) / 3600_000, instruments), from, to };
    if (statusCache.size > 8) statusCache.clear();
    statusCache.set(key, cached);
  }
  const t = now.getTime();
  const watching = cached.passes
    .filter((p) => p.start.getTime() <= t && t <= p.end.getTime())
    .map((p) => ({ name: p.satName, key: p.satKey, instrument: p.instrument, until: p.end }));
  const n = cached.passes.find((p) => p.start.getTime() > t);
  const l = cached.passes.filter((p) => p.end.getTime() < t).pop();
  return {
    watching,
    next: n ? { name: n.satName, key: n.satKey, instrument: n.instrument, at: n.tca, start: n.start, end: n.end } : null,
    last: l ? { name: l.satName, key: l.satKey, instrument: l.instrument, at: l.tca, end: l.end } : null,
  };
}

/** Current position of every satellite (for the moving markers). */
export function positions(sats: readonly Satellite[], at: Date): { sat: Satellite; lon: number; lat: number; heightKm: number }[] {
  const out: { sat: Satellite; lon: number; lat: number; heightKm: number }[] = [];
  for (const sat of sats) {
    const p = subpoint(sat, at);
    if (p) out.push({ sat, lon: p.lon, lat: p.lat, heightKm: p.heightKm });
  }
  return out;
}

/** Age of the element set: shown beside the map ("orbits from elements 9 h old"). */
export const elementAgeMs = (sat: Satellite, now: Date): number | null => (sat.epoch ? now.getTime() - sat.epoch.getTime() : null);
