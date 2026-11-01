import { PolygonLayer } from '@deck.gl/layers';

import { readGeoPixels, type GeostationaryLatestResponse, type GeoPixelProps } from '@/lib/api';
import { dateTimeIST, number } from '@/lib/format';
import type { Theme } from '@/lib/theme';

import { GEO_PIXEL_HALF_KM, paletteFor } from '../palette';
import { DATA_ANCHOR, type AnchorProps } from '../types';

type PixelDatum = { ring: [number, number][]; props: GeoPixelProps; centre: [number, number] };

const KM_PER_DEG = 111.32;

/** A square of `half` km around a pixel centre (when the API sends centres, not footprints). */
function squareAround([lon, lat]: [number, number], half: number): [number, number][] {
  const dLat = half / KM_PER_DEG;
  const dLon = half / (KM_PER_DEG * Math.cos((lat * Math.PI) / 180));
  return [
    [lon - dLon, lat - dLat],
    [lon + dLon, lat - dLat],
    [lon + dLon, lat + dLat],
    [lon - dLon, lat + dLat],
    [lon - dLon, lat - dLat],
  ];
}

export function geostationaryPixels(data: GeostationaryLatestResponse | null | undefined): PixelDatum[] {
  const out: PixelDatum[] = [];
  for (const f of readGeoPixels(data)) {
    const g = f.geometry;
    if (g.type === 'Point') {
      const c: [number, number] = [g.coordinates[0], g.coordinates[1]];
      out.push({ ring: squareAround(c, GEO_PIXEL_HALF_KM), props: f.properties ?? {}, centre: c });
    } else {
      const ring = (g.type === 'Polygon' ? g.coordinates[0] : g.coordinates[0]?.[0])?.map((p) => [p[0], p[1]] as [number, number]);
      if (!ring?.length) continue;
      const centre: [number, number] = [ring.reduce((s, p) => s + p[0], 0) / ring.length, ring.reduce((s, p) => s + p[1], 0) / ring.length];
      out.push({ ring, props: f.properties ?? {}, centre });
    }
  }
  return out;
}

/**
 * GK-2A fire pixels from /api/geostationary/latest: hollow squares about 4 km across, in the fire
 * colour. Geostationary sensors look every 10 minutes, so they see the evening burning the polar
 * satellites miss, but a pixel holds many fields: it is an evening fire-activity index, never a
 * fire count (SYNTHESIS.md, build-time updates).
 */
export function geostationaryLayer(data: GeostationaryLatestResponse | null | undefined, opts: { id?: string; visible?: boolean; pickable?: boolean; theme?: Theme } = {}) {
  const pixels = geostationaryPixels(data);
  if (!pixels.length) return null;
  // constant colours: deck.gl compares those itself, so a theme change repaints without a trigger
  const { GEO_PIXEL_STYLE: S, HIGHLIGHT } = paletteFor(opts.theme);
  return new PolygonLayer<PixelDatum, AnchorProps>({
    id: opts.id ?? 'geostationary',
    beforeId: DATA_ANCHOR,
    data: pixels,
    visible: opts.visible ?? true,
    pickable: opts.pickable ?? true,
    autoHighlight: true,
    highlightColor: HIGHLIGHT,
    filled: true,
    stroked: true,
    lineWidthUnits: 'pixels',
    getPolygon: (d) => d.ring,
    getFillColor: S.fill,
    getLineColor: S.stroke,
    getLineWidth: S.strokePx,
  });
}

export function describeGeoPixel(d: PixelDatum, data?: GeostationaryLatestResponse | null): string {
  const scan = d.props.scan_utc ?? data?.scan_utc;
  return [
    `GK-2A fire pixel (about 4 km across)`,
    scan ? `Scan ${dateTimeIST(scan, { suffix: true })}` : null,
    d.props.btd_excess != null ? `${number(d.props.btd_excess, { decimals: 1 })} K hotter than its surroundings at 3.8 µm` : null,
    'An activity index, not a count of fires',
  ]
    .filter(Boolean)
    .join('\n');
}
