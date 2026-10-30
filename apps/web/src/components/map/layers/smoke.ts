import { TripsLayer } from '@deck.gl/geo-layers';
import { GeoJsonLayer, PathLayer } from '@deck.gl/layers';

import type { AreaGeometry, Feature, SmokeBackResponse, SmokeForwardResponse } from '@/lib/api';
import { fromUnixSeconds } from '@/lib/format';

import type { ClockFrame } from '../clock';
import { SMOKE_STYLE, type RGBA } from '../palette';
import { DATA_ANCHOR, type AnchorProps, type AnimatedLayer, type MapLayerInput } from '../types';

type Trail = { id: string; path: [number, number][]; t: number[]; weight: number; model?: string };
type Prepared = { trails: Trail[]; t0: number; duration: number; maxWeight: number };

const prepared = new WeakMap<object, Prepared | null>();

/**
 * Normalise the run once per response: points in time order (back-trajectories arrive newest
 * first and are turned around, so the animation shows air arriving at the city), times as seconds
 * since the run's first moment (unix seconds would lose precision in the GPU's 32-bit floats).
 */
function prepare(data: SmokeForwardResponse | SmokeBackResponse): Prepared | null {
  const hit = prepared.get(data);
  if (hit !== undefined) return hit;
  const trails: Trail[] = [];
  let t0 = Infinity;
  let tEnd = -Infinity;
  let maxWeight = 0;
  for (const tr of data.trails ?? []) {
    const n = Math.min(tr.path?.length ?? 0, tr.times?.length ?? 0);
    if (n < 2) continue;
    const pts = Array.from({ length: n }, (_, i) => ({ p: tr.path[i], t: tr.times[i] }))
      .filter((x) => Number.isFinite(x.t) && Number.isFinite(x.p?.[0]) && Number.isFinite(x.p?.[1]))
      .sort((a, b) => a.t - b.t);
    if (pts.length < 2) continue;
    t0 = Math.min(t0, pts[0].t);
    tEnd = Math.max(tEnd, pts[pts.length - 1].t);
    maxWeight = Math.max(maxWeight, tr.weight ?? 0);
    trails.push({ id: String(tr.id), path: pts.map((x) => [x.p[0], x.p[1]]), t: pts.map((x) => x.t), weight: tr.weight ?? 1, model: tr.model });
  }
  const out = trails.length && Number.isFinite(t0) ? { trails: trails.map((tr) => ({ ...tr, t: tr.t.map((x) => x - t0) })), t0, duration: Math.max(1, tEnd - t0), maxWeight: maxWeight || 1 } : null;
  prepared.set(data, out);
  return out;
}

export type SmokeLayerOptions = {
  id?: string;
  visible?: boolean;
  /** Seconds the whole run takes to play (default 18). */
  loopSeconds?: number;
  /** Length of the fading tail, in hours of the run (default 3). */
  trailHours?: number;
  /** Draw the spread between the two forecast models when the API sends it (default true). */
  showCone?: boolean;
};

/** Where the animation clock `t` (seconds) is in the run: the simulated moment and how far along. */
export function smokeTimeAt(data: SmokeForwardResponse | SmokeBackResponse | null | undefined, t: number, opts: SmokeLayerOptions = {}): { at: Date; progress: number } | null {
  const prep = data ? prepare(data) : null;
  if (!prep) return null;
  const loop = opts.loopSeconds ?? SMOKE_STYLE.loopSeconds;
  const trail = (opts.trailHours ?? SMOKE_STYLE.trailHours) * 3600;
  const cycle = prep.duration + trail;
  const sim = ((t * prep.duration) / loop) % cycle;
  const clamped = Math.min(sim, prep.duration);
  return { at: fromUnixSeconds(prep.t0 + clamped), progress: clamped / prep.duration };
}

/**
 * Smoke trajectories (/api/smoke/forward or /api/smoke/back) as moving trails: each trail's head
 * advances with the shared clock and fades behind it; width and opacity follow the trail's weight.
 * Under reduced motion the whole paths are drawn still. When the API sends an ensemble cone it is
 * drawn faintly beneath, because no single path is the truth.
 */
export function smokeLayers(data: SmokeForwardResponse | SmokeBackResponse | null | undefined, opts: SmokeLayerOptions = {}): MapLayerInput[] {
  const prep = data ? prepare(data) : null;
  if (!data || !prep) return [];
  const id = opts.id ?? 'smoke';
  const visible = opts.visible ?? true;
  const loop = opts.loopSeconds ?? SMOKE_STYLE.loopSeconds;
  const trail = (opts.trailHours ?? SMOKE_STYLE.trailHours) * 3600;
  const cycle = prep.duration + trail;
  const [r, g, b, a] = SMOKE_STYLE.color;
  const colour = (d: Trail): RGBA => [r, g, b, Math.round(a * (0.45 + 0.55 * Math.sqrt(d.weight / prep.maxWeight)))];
  const width = (d: Trail) => SMOKE_STYLE.minWidthPx + (SMOKE_STYLE.maxWidthPx - SMOKE_STYLE.minWidthPx) * Math.sqrt(d.weight / prep.maxWeight);
  const getPath = (d: Trail) => d.path;
  const getTimestamps = (d: Trail) => d.t;

  const coneFeatures = (data.cone?.features ?? []) as Feature<AreaGeometry, Record<string, unknown>>[];
  const cone =
    (opts.showCone ?? true) && coneFeatures.length
      ? new GeoJsonLayer<Record<string, unknown>, AnchorProps>({
          id: `${id}:cone`,
          beforeId: DATA_ANCHOR,
          data: coneFeatures,
          visible,
          filled: true,
          stroked: false,
          getFillColor: SMOKE_STYLE.coneFill,
          pickable: false,
        })
      : null;

  const trails: AnimatedLayer = {
    id: `${id}:trails`,
    animated: true,
    render: (frame: ClockFrame) => {
      if (frame.reduced) {
        return new PathLayer<Trail, AnchorProps>({
          id: `${id}:trails`,
          beforeId: DATA_ANCHOR,
          data: prep.trails,
          visible,
          widthUnits: 'pixels',
          getPath,
          getColor: (d) => {
            const c = colour(d);
            return [c[0], c[1], c[2], Math.round(c[3] * 0.6)];
          },
          getWidth: width,
          capRounded: true,
          jointRounded: true,
        });
      }
      return new TripsLayer<Trail, AnchorProps>({
        id: `${id}:trails`,
        beforeId: DATA_ANCHOR,
        data: prep.trails,
        visible,
        widthUnits: 'pixels',
        getPath,
        getTimestamps,
        getColor: colour,
        getWidth: width,
        capRounded: true,
        jointRounded: true,
        fadeTrail: true,
        trailLength: trail,
        currentTime: ((frame.t * prep.duration) / loop) % cycle,
      });
    },
  };
  return [cone, trails];
}
