/**
 * Point-in-polygon on [lon, lat] rings, planar in degrees: close enough at a district's or a field's
 * scale. Nothing here imports deck.gl or MapLibre, so the parts of /live that paint before the map
 * (the case summary) can use it without loading them.
 */

export type Ring = [number, number][];

/** Whether (x, y) lies inside a ring: ray casting. */
function inRing(ring: Ring, x: number, y: number): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Inside one of a Polygon's or MultiPolygon's polygons: within its outer ring and outside its holes. */
export function inShape(g: { type: string; coordinates: unknown } | null | undefined, x: number, y: number): boolean {
  if (!g) return false;
  const polys: Ring[][] = g.type === 'Polygon' ? [g.coordinates as Ring[]] : g.type === 'MultiPolygon' ? (g.coordinates as Ring[][]) : [];
  return polys.some((p) => !!p[0] && inRing(p[0], x, y) && !p.slice(1).some((hole) => inRing(hole, x, y)));
}
