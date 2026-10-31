import { serverGet } from '@/lib/api-server';
import { PATHS } from '@/lib/api';

import type { StateBoundaries } from './types';

/**
 * Where the case is: Punjab, Haryana, Chandigarh and Delhi drawn from the Survey of India state
 * polygons the API serves (never a basemap outline), with the scar as a dot. A Server Component:
 * the path data is computed once on the server (simplified to 0.02 degrees by the API, rounded to
 * 0.1 of a drawing unit here) and ships as plain SVG.
 */

const STATES = ['Punjab', 'Haryana', 'Chandigarh', 'Delhi'];
const W = 400;

export async function Locator({ lon, lat, state }: { lon: number; lat: number; state: string | null }) {
  const r = await serverGet<StateBoundaries>(PATHS.geoBoundaries, { level: 'state', simplify: 0.02 }, 12_000);
  if (!r.ok) return <p className="card__note">The state outlines could not be loaded: {r.error.message}</p>;
  const feats = (r.data.features?.features ?? []).filter((f) => STATES.includes(f.properties?.name));
  if (!feats.length) return <p className="card__note">The API returned no state outlines for this region.</p>;

  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const polys = feats.map((f) => (f.geometry.type === 'Polygon' ? [f.geometry.coordinates] : f.geometry.coordinates));
  for (const mp of polys) for (const poly of mp) for (const ring of poly) for (const [lo, la] of ring) {
    x0 = Math.min(x0, lo); x1 = Math.max(x1, lo); y0 = Math.min(y0, la); y1 = Math.max(y1, la);
  }
  const k = Math.cos((((y0 + y1) / 2) * Math.PI) / 180);
  const scale = W / ((x1 - x0) * k);
  const H = Math.round((y1 - y0) * scale);
  const px = (lo: number) => ((lo - x0) * k * scale).toFixed(1);
  const py = (la: number) => ((y1 - la) * scale).toFixed(1);
  const pathOf = (mp: number[][][][]) => mp.map((poly) => poly.map((ring) => ring.map(([lo, la], i) => `${i ? 'L' : 'M'}${px(lo)} ${py(la)}`).join('') + 'Z').join('')).join('');

  return (
    <figure className="locator">
      <svg viewBox={`-6 -6 ${W + 12} ${H + 12}`} role="img" aria-label={`Map of Punjab, Haryana, Chandigarh and Delhi with the case marked in ${state}.`}>
        {feats.map((f, i) => (
          <path key={f.properties.name} d={pathOf(polys[i])} fill={f.properties.name === state ? 'var(--raised)' : 'var(--surface)'} stroke="var(--control)" strokeWidth={1} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
        ))}
        <circle cx={px(lon)} cy={py(lat)} r={9} fill="none" stroke="var(--accent)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        <circle cx={px(lon)} cy={py(lat)} r={3} fill="var(--accent)" />
      </svg>
      {r.data.attribution ? <figcaption className="card__note">{r.data.attribution.split('. ')[0]}.</figcaption> : null}
    </figure>
  );
}
