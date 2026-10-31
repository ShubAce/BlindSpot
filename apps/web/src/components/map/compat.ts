import { setWorkerUrl, type Map as MapLibreMap } from 'maplibre-gl';

/**
 * Two seams between MapLibre 6 and deck.gl 9.4, fixed in one place for every map.
 *
 * 1. The worker. MapLibre 6 finds its worker next to its own module (`import.meta.url` +
 *    "maplibre-gl-worker.mjs"), a file bundlers do not copy, so every map failed with "Worker failed
 *    to load". `ensureMapWorker` points it at the worker as an asset the bundler emits
 *    (`new URL(..., import.meta.url)`); the worker file is self-contained.
 * 2. The camera. deck.gl's interleaved overlay reads `map.transform` (its height, near and far
 *    planes, elevation), which MapLibre 6 moved to the map's camera. `patchMapForDeck` gives the map
 *    that property back as a read-only view of the same camera state, so deck.gl draws again.
 */

let workerSet = false;
export function ensureMapWorker(): void {
  if (workerSet || typeof window === 'undefined') return;
  workerSet = true;
  setWorkerUrl(new URL('../../../node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs', import.meta.url).href);
}

type Camera = { transform?: unknown };

export function patchMapForDeck(map: MapLibreMap | null | undefined): void {
  if (!map || 'transform' in map) return;
  Object.defineProperty(map, 'transform', {
    configurable: true,
    get(this: { _camera?: Camera }) {
      return this._camera?.transform;
    },
  });
}
