import type { Layer } from '@deck.gl/core';
import { ScatterplotLayer, TextLayer } from '@deck.gl/layers';

import type { AirLatestResponse, AirStation } from '@/lib/api';
import { dateTimeIST, metricText, number, placeName } from '@/lib/format';

import { AIR_STYLE, HIGHLIGHT, NAQI_BANDS, naqiBand } from '../palette';
import { DATA_ANCHOR, type AnchorProps } from '../types';
import { mapLabelFont } from './common';

export type AirLayerOptions = {
  id?: string;
  pickable?: boolean;
  visible?: boolean;
  labels?: boolean;
  /** Hide readings older than this many minutes (default 360). */
  maxAgeMinutes?: number;
};

const unitOf = (s: AirStation) => s.pm25_unit ?? s.unit ?? null;

/** A reading older than this is not drawn as "now": many CPCB feeds lag by hours, some by days. */
const DEFAULT_MAX_AGE_MIN = 6 * 60;

/**
 * Ground stations from /api/air/latest: one dot per station on the six CPCB NAQI bands (whiter is
 * worse), ringed in the ground colour; model values (CAMS) are rings with no fill, so a model is
 * never read as a measurement. Each station's PM2.5 is written beside it.
 */
export function airLayers(data: AirLatestResponse | null | undefined, opts: AirLayerOptions = {}): Layer[] {
  const maxAge = opts.maxAgeMinutes ?? DEFAULT_MAX_AGE_MIN;
  const stations = (data?.stations ?? []).filter(
    (s) => Number.isFinite(s.lat) && Number.isFinite(s.lon) && s.pm25 != null && !(s.age_minutes != null && s.age_minutes > maxAge),
  );
  if (!stations.length) return [];
  const id = opts.id ?? 'air';
  const colour = (s: AirStation) => AIR_STYLE.ramp[naqiBand(s.pm25 ?? 0, unitOf(s))];
  const layers: Layer[] = [
    new ScatterplotLayer<AirStation, AnchorProps>({
      id: `${id}:stations`,
      beforeId: DATA_ANCHOR,
      data: stations,
      visible: opts.visible ?? true,
      pickable: opts.pickable ?? true,
      autoHighlight: true,
      highlightColor: HIGHLIGHT,
      radiusUnits: 'pixels',
      lineWidthUnits: 'pixels',
      stroked: true,
      filled: true,
      getPosition: (s) => [s.lon, s.lat],
      getRadius: AIR_STYLE.radiusPx,
      getFillColor: (s) => (s.is_model ? [0, 0, 0, 0] : colour(s)),
      getLineColor: (s) => (s.is_model ? colour(s) : AIR_STYLE.stroke),
      getLineWidth: AIR_STYLE.strokePx,
    }),
  ];
  if (opts.labels ?? true) {
    layers.push(
      new TextLayer<AirStation, AnchorProps>({
        id: `${id}:values`,
        beforeId: DATA_ANCHOR,
        data: stations,
        visible: opts.visible ?? true,
        pickable: false,
        getPosition: (s) => [s.lon, s.lat],
        getText: (s) => number(s.pm25),
        getSize: AIR_STYLE.labelPx,
        sizeUnits: 'pixels',
        getColor: AIR_STYLE.labelColor,
        getTextAnchor: 'start',
        getAlignmentBaseline: 'center',
        getPixelOffset: [AIR_STYLE.radiusPx + 4, 0],
        fontFamily: mapLabelFont(),
        fontWeight: 500,
        characterSet: '0123456789,.-−—',
        fontSettings: { sdf: true },
        outlineWidth: 3,
        outlineColor: AIR_STYLE.labelOutline,
      }),
    );
  }
  return layers;
}

export function describeStation(s: AirStation): string {
  const unit = unitOf(s);
  const isIndex = !!unit && /index|aqi/i.test(unit);
  const value = s.pm25 == null ? null : isIndex ? `PM2.5 sub-index ${number(s.pm25)}` : `PM2.5 ${metricText('pm25', s.pm25)}`;
  return [
    s.name,
    placeName(s.city, s.state),
    value ? `${value} (${NAQI_BANDS[naqiBand(s.pm25 ?? 0, unit)]})` : null,
    s.is_model ? 'Model estimate (CAMS), not a measurement' : `Measured, ${s.provider}`,
    s.observed_at ? dateTimeIST(s.observed_at, { suffix: true }) : null,
  ]
    .filter(Boolean)
    .join('\n');
}
