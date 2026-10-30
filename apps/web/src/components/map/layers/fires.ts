import { ScatterplotLayer } from '@deck.gl/layers';

import type { Feature, FireProps, FiresResponse, PointGeometry } from '@/lib/api';
import { dateTimeIST, metricText, placeName } from '@/lib/format';

import { FIRE_STYLE, fireRadiusPx, HIGHLIGHT } from '../palette';
import { DATA_ANCHOR, type AnchorProps } from '../types';

export type FireFeature = Feature<PointGeometry, FireProps>;

export type FiresLayerOptions = {
  id?: string;
  /** With a clock, detections older than six hours are drawn dimmer (legend: "last 6 h" / "6-24 h"). */
  now?: Date | null;
  pickable?: boolean;
  visible?: boolean;
  opacity?: number;
};

/**
 * NASA FIRMS detections (VIIRS 375 m, MODIS 1 km) from /api/fires: one dot per detection, area
 * proportional to fire radiative power, ringed in the ground colour so neighbours stay apart.
 * Returns null when there is nothing to draw (the caller shows the empty sentence).
 */
export function firesLayer(data: FiresResponse | null | undefined, opts: FiresLayerOptions = {}) {
  const features = (data?.features?.features ?? []).filter((f) => f?.geometry?.coordinates?.length >= 2);
  if (!features.length) return null;
  // Big fires first, so a small one beside a big one stays on top and visible.
  const sorted = features.slice().sort((a, b) => (b.properties.frp_mw ?? 0) - (a.properties.frp_mw ?? 0));
  const nowMs = opts.now ? opts.now.getTime() : null;
  const recentMs = FIRE_STYLE.recentHours * 3600_000;
  return new ScatterplotLayer<FireFeature, AnchorProps>({
    id: opts.id ?? 'fires',
    beforeId: DATA_ANCHOR,
    data: sorted,
    visible: opts.visible ?? true,
    opacity: opts.opacity ?? 1,
    pickable: opts.pickable ?? true,
    autoHighlight: true,
    highlightColor: HIGHLIGHT,
    radiusUnits: 'pixels',
    lineWidthUnits: 'pixels',
    stroked: true,
    filled: true,
    getPosition: (f) => [f.geometry.coordinates[0], f.geometry.coordinates[1]],
    getRadius: (f) => fireRadiusPx(f.properties.frp_mw),
    getFillColor: (f) => (nowMs != null && nowMs - Date.parse(f.properties.acq_utc) > recentMs ? FIRE_STYLE.fillOld : FIRE_STYLE.fill),
    getLineColor: FIRE_STYLE.stroke,
    getLineWidth: FIRE_STYLE.strokePx,
    // Re-colour the age classes every ten minutes, not every render.
    updateTriggers: { getFillColor: [nowMs == null ? 0 : Math.floor(nowMs / 600_000)] },
  });
}

/** Tooltip text for one detection. */
export function describeFire(f: FireFeature): string {
  const p = f.properties;
  const lines = [
    `${p.satellite}${p.instrument ? ` (${p.instrument})` : ''}, ${dateTimeIST(p.acq_utc, { suffix: true })}`,
    [p.frp_mw != null ? `${metricText('frp_mw', p.frp_mw)} fire power` : null, p.confidence ? `${p.confidence} confidence` : null].filter(Boolean).join(', '),
    placeName(p.district, p.state),
  ];
  return lines.filter(Boolean).join('\n');
}
