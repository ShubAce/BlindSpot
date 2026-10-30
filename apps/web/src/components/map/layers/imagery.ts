import { TileLayer } from '@deck.gl/geo-layers';
import { BitmapLayer } from '@deck.gl/layers';

import { AOI_BBOX, type BBox } from '@/lib/aoi';
import { imageryTileTemplate, type ImageryKind } from '@/lib/api';

import { DATA_ANCHOR, type AnchorProps } from '../types';

export type ImageryLayerOptions = {
  /** STAC item id of one Sentinel-2 scene (from /api/imagery/featured or a case file). */
  item: string;
  /** truecolor (as the eye sees it), swir (burnt ground dark red), nbr (burn ratio). Default truecolor. */
  kind?: ImageryKind;
  id?: string;
  visible?: boolean;
  opacity?: number;
  /** Only request tiles inside this box (default: the AOI). */
  bounds?: BBox;
  minZoom?: number;
  maxZoom?: number;
  /** Called when a tile fails, so the page can say so instead of leaving a silent hole. */
  onTileError?: (error: unknown) => void;
};

type TileImage = ImageBitmap | null;

/**
 * One Sentinel-2 scene, as tiles proxied and cached by the API (/api/imagery/tiles/...). Tiles are
 * fetched with the tile's own abort signal (panning away cancels them) and decoded off the main
 * thread with createImageBitmap. 10 m pixels: tiles below zoom 8 are not requested.
 */
export function imageryLayer(opts: ImageryLayerOptions) {
  const template = imageryTileTemplate(opts.kind ?? 'truecolor', opts.item);
  const b = opts.bounds ?? AOI_BBOX;
  return new TileLayer<TileImage, AnchorProps>({
    id: opts.id ?? `imagery:${opts.kind ?? 'truecolor'}:${opts.item}`,
    beforeId: DATA_ANCHOR,
    data: template,
    visible: opts.visible ?? true,
    opacity: opts.opacity ?? 1,
    minZoom: opts.minZoom ?? 8,
    maxZoom: opts.maxZoom ?? 14,
    tileSize: 256,
    extent: [b[0], b[1], b[2], b[3]],
    maxRequests: 6,
    refinementStrategy: 'no-overlap',
    pickable: false,
    onTileError: (err: unknown) => opts.onTileError?.(err),
    getTileData: async ({ index, signal }) => {
      const url = template.replace('{z}', String(index.z)).replace('{x}', String(index.x)).replace('{y}', String(index.y));
      const res = await fetch(url, { signal });
      if (res.status === 204 || res.status === 404) return null; // no imagery for this tile
      if (!res.ok) throw new Error(`imagery tile ${index.z}/${index.x}/${index.y}: ${res.status}`);
      return createImageBitmap(await res.blob());
    },
    renderSubLayers: (props) => {
      const { tile, data } = props;
      if (!data) return null;
      const bb = tile.boundingBox;
      return new BitmapLayer({
        id: `${props.id}-bitmap`,
        image: data,
        bounds: [bb[0][0], bb[0][1], bb[1][0], bb[1][1]],
        opacity: props.opacity,
      });
    },
  });
}
