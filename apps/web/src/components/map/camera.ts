import type { MapRef } from 'react-map-gl/maplibre';

import { mapClock } from './clock';

export type CameraTarget =
  | { lon: number; lat: number; zoom?: number }
  | { bounds: [number, number, number, number]; padding?: number };

/**
 * Move the camera (an agent's `map` event with action "fly_to", a click in a ranked list). Glides
 * over 1.2 s, or jumps when the viewer prefers reduced motion.
 */
export function flyTo(map: MapRef | null | undefined, target: CameraTarget): void {
  if (!map) return;
  const duration = mapClock.isReduced ? 0 : 1200;
  if ('bounds' in target) {
    const [w, s, e, n] = target.bounds;
    map.fitBounds([w, s, e, n], { padding: target.padding ?? 48, duration, maxZoom: 13 });
    return;
  }
  map.flyTo({ center: [target.lon, target.lat], zoom: target.zoom ?? Math.max(map.getZoom(), 10), duration, essential: false });
}
