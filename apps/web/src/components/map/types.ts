import type { Layer } from '@deck.gl/core';

import type { ClockFrame } from './clock';

/** A layer that changes every animation frame (smoke trails, wind). The map calls `render` from
 *  its animation loop; build the deck.gl layer cheaply there (accessors and data stay the same,
 *  only `currentTime` changes). */
export type AnimatedLayer = {
  id: string;
  animated: true;
  render: (frame: ClockFrame) => Layer | null;
};

/** What BaseMap accepts: deck.gl layers, animated layers, nested lists, and gaps (null/false) so a
 *  page can write `[showFires && firesLayer(data)]`. */
export type MapLayerInput = Layer | AnimatedLayer | null | undefined | false | readonly MapLayerInput[];

export const isAnimated = (x: unknown): x is AnimatedLayer =>
  !!x && typeof x === 'object' && (x as { animated?: unknown }).animated === true && typeof (x as { render?: unknown }).render === 'function';

/** Every deck.gl layer goes into the basemap's layer stack just below this invisible anchor:
 *  above the land, water and roads, below our Survey of India boundaries and the place labels. */
export const DATA_ANCHOR = 'bs-anchor-data';
/** Boundary lines are inserted below this anchor (it sits right after DATA_ANCHOR). */
export const BOUNDARY_ANCHOR = 'bs-anchor-boundaries';

/** Extra props deck.gl's MapboxOverlay reads in interleaved mode. */
export type AnchorProps = { beforeId?: string };
