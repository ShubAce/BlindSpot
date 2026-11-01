import { TripsLayer } from '@deck.gl/geo-layers';
import { PathLayer } from '@deck.gl/layers';

import type { WindFieldResponse } from '@/lib/api';
import type { Theme } from '@/lib/theme';

import type { ClockFrame } from '../clock';
import { paletteFor, type RGBA } from '../palette';
import { DATA_ANCHOR, type AnchorProps, type AnimatedLayer } from '../types';

/** A wind grid as flat arrays: u/v in m/s at lon0 + i*dlon, lat0 + j*dlat, index j*nx + i. */
export type WindField = { lon0: number; lat0: number; dlon: number; dlat: number; nx: number; ny: number; u: Float32Array; v: Float32Array; level: string };

function flat(a: number[] | number[][] | undefined): Float32Array | null {
  if (!Array.isArray(a)) return null;
  const rows = Array.isArray(a[0]) ? (a as number[][]).flat() : (a as number[]);
  return Float32Array.from(rows, (x) => (x == null ? NaN : Number(x)));
}

/** The grid from /api/met/wind-field, or null if it is not a complete nx by ny grid. */
export function readWindField(r: WindFieldResponse | null | undefined): WindField | null {
  const g = r?.grid;
  if (!g || !(g.nx > 1) || !(g.ny > 1) || !g.dlon || !g.dlat) return null;
  const u = flat(r.u);
  const v = flat(r.v);
  if (!u || !v || u.length !== g.nx * g.ny || v.length !== g.nx * g.ny) return null;
  return { ...g, u, v, level: r.level };
}

/** Bilinear wind at a point, or null outside the grid or where values are missing. */
export function sampleWind(f: WindField, lon: number, lat: number): [number, number] | null {
  const fx = (lon - f.lon0) / f.dlon;
  const fy = (lat - f.lat0) / f.dlat;
  if (!(fx >= 0 && fy >= 0 && fx <= f.nx - 1 && fy <= f.ny - 1)) return null;
  const i = Math.min(f.nx - 2, Math.floor(fx));
  const j = Math.min(f.ny - 2, Math.floor(fy));
  const tx = fx - i;
  const ty = fy - j;
  const at = (arr: Float32Array, ii: number, jj: number) => arr[jj * f.nx + ii];
  const lerp = (arr: Float32Array) =>
    (1 - ty) * ((1 - tx) * at(arr, i, j) + tx * at(arr, i + 1, j)) + ty * ((1 - tx) * at(arr, i, j + 1) + tx * at(arr, i + 1, j + 1));
  const u = lerp(f.u);
  const v = lerp(f.v);
  return Number.isFinite(u) && Number.isFinite(v) ? [u, v] : null;
}

/** Halton low-discrepancy sequence: evenly spread seeds without randomness, the same every render. */
function halton(index: number, base: number): number {
  let f = 1;
  let r = 0;
  let i = index;
  while (i > 0) {
    f /= base;
    r += f * (i % base);
    i = Math.floor(i / base);
  }
  return r;
}

export type Streamline = { path: [number, number][]; t: number[]; speed: number };

const M_PER_DEG = 111_320;

/**
 * Advect evenly spread seed points through the (frozen) wind field with a midpoint (RK2) step, so
 * each path follows the flow for `steps` x `stepMinutes`. Every path is emitted twice, one period
 * apart in time, so the looping animation never empties: as one copy ends, its twin is mid-way.
 */
export function windStreamlines(f: WindField, opts: { seeds?: number; stepMinutes?: number; steps?: number; minSpeed?: number } = {}): { lines: Streamline[]; period: number } {
  const seeds = opts.seeds ?? 900;
  const dt = (opts.stepMinutes ?? 30) * 60;
  const steps = opts.steps ?? 16;
  const minSpeed = opts.minSpeed ?? 0.5;
  const period = steps * dt;
  const lonA = f.lon0;
  const lonB = f.lon0 + (f.nx - 1) * f.dlon;
  const latA = f.lat0;
  const latB = f.lat0 + (f.ny - 1) * f.dlat;
  const [lonMin, lonMax] = [Math.min(lonA, lonB), Math.max(lonA, lonB)];
  const [latMin, latMax] = [Math.min(latA, latB), Math.max(latA, latB)];
  const move = (lon: number, lat: number, u: number, v: number, s: number): [number, number] => [
    lon + (u * s) / (M_PER_DEG * Math.cos((lat * Math.PI) / 180)),
    lat + (v * s) / M_PER_DEG,
  ];

  const lines: Streamline[] = [];
  for (let k = 1; k <= seeds; k++) {
    let lon = lonMin + halton(k, 2) * (lonMax - lonMin);
    let lat = latMin + halton(k, 3) * (latMax - latMin);
    const phase = halton(k, 5) * period;
    const path: [number, number][] = [[lon, lat]];
    let speedSum = 0;
    for (let s = 0; s < steps; s++) {
      const k1 = sampleWind(f, lon, lat);
      if (!k1) break;
      const sp = Math.hypot(k1[0], k1[1]);
      if (sp < minSpeed) break;
      const mid = move(lon, lat, k1[0], k1[1], dt / 2);
      const k2 = sampleWind(f, mid[0], mid[1]);
      if (!k2) break;
      [lon, lat] = move(lon, lat, k2[0], k2[1], dt);
      path.push([lon, lat]);
      speedSum += Math.hypot(k2[0], k2[1]);
    }
    if (path.length < 3) continue;
    const speed = speedSum / (path.length - 1);
    const times = path.map((_, i) => i * dt);
    lines.push({ path, t: times.map((x) => x + phase), speed });
    lines.push({ path, t: times.map((x) => x + phase - period), speed });
  }
  return { lines, period };
}

const cache = new WeakMap<object, { field: WindField; lines: Streamline[]; period: number } | null>();

export type WindLayerOptions = {
  id?: string;
  visible?: boolean;
  seeds?: number;
  loopSeconds?: number;
  /** Colours for this theme's basemap (default dark). */
  theme?: Theme;
};

/**
 * Wind (/api/met/wind-field, 925 hPa by default: the level the smoke rides) as faint streaks that
 * flow along the field. Under reduced motion, the streamlines are drawn still.
 */
export function windLayer(data: WindFieldResponse | null | undefined, opts: WindLayerOptions = {}): AnimatedLayer | null {
  if (!data) return null;
  let entry = cache.get(data);
  if (entry === undefined) {
    const field = readWindField(data);
    entry = field ? { field, ...windStreamlines(field, { seeds: opts.seeds }) } : null;
    cache.set(data, entry);
  }
  if (!entry || !entry.lines.length) return null;
  const { lines, period } = entry;
  const id = opts.id ?? 'wind';
  const visible = opts.visible ?? true;
  const { WIND_STYLE, theme } = paletteFor(opts.theme);
  const loop = opts.loopSeconds ?? WIND_STYLE.loopSeconds;
  const [r, g, b, a] = WIND_STYLE.color;
  // a new layer every frame, with new accessor functions deck.gl never compares: the theme is what
  // tells it the streaks' colours changed
  const updateTriggers = { getColor: [theme] };
  const colour = (d: Streamline): RGBA => [r, g, b, Math.round(a * (0.35 + 0.65 * Math.min(1, d.speed / 12)))];
  const getPath = (d: Streamline) => d.path;
  const getTimestamps = (d: Streamline) => d.t;

  return {
    id: `${id}:streaks`,
    animated: true,
    render: (frame: ClockFrame) =>
      frame.reduced
        ? new PathLayer<Streamline, AnchorProps>({
            id: `${id}:streaks`,
            beforeId: DATA_ANCHOR,
            // one copy of each line is enough when nothing moves
            data: lines.filter((_, i) => i % 2 === 0),
            visible,
            widthUnits: 'pixels',
            getPath,
            getColor: (d) => {
              const c = colour(d);
              return [c[0], c[1], c[2], Math.round(c[3] * 0.5)];
            },
            getWidth: WIND_STYLE.widthPx,
            updateTriggers,
          })
        : new TripsLayer<Streamline, AnchorProps>({
            id: `${id}:streaks`,
            beforeId: DATA_ANCHOR,
            data: lines,
            visible,
            widthUnits: 'pixels',
            getPath,
            getTimestamps,
            getColor: colour,
            getWidth: WIND_STYLE.widthPx,
            updateTriggers,
            fadeTrail: true,
            trailLength: period * 0.35,
            currentTime: ((frame.t * period) / loop) % period,
          }),
  };
}
