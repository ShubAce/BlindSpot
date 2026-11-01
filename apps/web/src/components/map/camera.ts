import type { MapRef } from 'react-map-gl/maplibre';

import { mapClock } from './clock';

/** Where to move the camera. `instant` jumps without the glide: a page opening on a place (a
 *  location link) starts there instead of flying in over a map that is still drawing. */
export type CameraTarget =
  | { lon: number; lat: number; zoom?: number; instant?: boolean }
  | { bounds: [number, number, number, number]; padding?: number; instant?: boolean };

/**
 * Move the camera (an agent's `map` event with action "fly_to", a click in a ranked list). Glides
 * over 1.2 s, or jumps when the viewer prefers reduced motion or the target asks for it.
 */
export function flyTo(map: MapRef | null | undefined, target: CameraTarget): void {
  if (!map) return;
  const duration = target.instant || mapClock.isReduced ? 0 : 1200;
  if ('bounds' in target) {
    const [w, s, e, n] = target.bounds;
    map.fitBounds([w, s, e, n], { padding: target.padding ?? 48, duration, maxZoom: 13 });
    return;
  }
  map.flyTo({ center: [target.lon, target.lat], zoom: target.zoom ?? Math.max(map.getZoom(), 10), duration, essential: false });
}
