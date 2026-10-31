import type { Layer } from '@deck.gl/core';
import { PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';

import { timeIST } from '@/lib/format';
import { FIRE_INSTRUMENTS, groundTrack, positions, subpoint, swathForPass, swathPolygons, type LatLon, type PassWindow, type Ring, type Satellite } from '@/lib/orbits';

import { nextLookParts } from '../look';
import { SATELLITE_STYLE } from '../palette';
import { DATA_ANCHOR, TOP_ANCHOR, type AnchorProps } from '../types';
import { mapLabelFont } from './common';

export type SatelliteLayerOptions = {
  id?: string;
  visible?: boolean;
  /** Ground tracks 50 minutes either side of now (default true). */
  tracks?: boolean;
  /** The strip each satellite images in the 6 minutes either side of now (default true). */
  swaths?: boolean;
  /** Footprints of chosen passes over a site, labelled with their IST time (e.g. the last and the
   *  next look at Punjab), so the map says when an area was last seen even when no satellite is near. */
  passes?: readonly PassWindow[];
  /** Restrict to these instruments (default: all the API sends). */
  instruments?: readonly string[];
  labels?: boolean;
  /** The next look at a site, drawn so "who is watching" shows on the map: the line its scanner
   *  sweeps through the site at closest approach (across the whole swath), the pass's ground track
   *  either side of that moment, and a label with the time and how far off the track the site lies. */
  nextLook?: { pass: PassWindow; site: LatLon; /** Draw its label here (default true); false when the
   *  page writes it as a basemap symbol instead, so town names make way for it. */ label?: boolean } | null;
};

type TrackDatum = { sat: Satellite; path: [number, number][] };
type RingDatum = { sat: Satellite; ring: Ring; label?: string };

const isFire = (s: Satellite) => FIRE_INSTRUMENTS.includes(s.instrument);

/**
 * Satellites from /api/orbits/tle, propagated in the browser (lib/orbits.ts): live position, ground
 * track and the swath it is imaging, plus the footprints of chosen passes. Redraw with a fresh `now`
 * (a 1 s clock is plenty: a satellite moves about 7 km a second, under 2 px at state zoom).
 * Sentinel-2 is drawn fainter: it photographs the fields, it does not count fires.
 */
export function satelliteLayers(sats: readonly Satellite[], now: Date | null, opts: SatelliteLayerOptions = {}): Layer[] {
  if (!now || !sats.length) return [];
  const id = opts.id ?? 'satellites';
  const visible = opts.visible ?? true;
  const chosen = opts.instruments ? sats.filter((s) => opts.instruments?.includes(s.instrument)) : sats.slice();
  const out: Layer[] = [];

  const ringData: RingDatum[] = [];
  if (opts.swaths ?? true) {
    for (const sat of chosen) {
      for (const ring of swathPolygons(sat, new Date(now.getTime() - 6 * 60_000), new Date(now.getTime() + 6 * 60_000))) ringData.push({ sat, ring });
    }
  }
  const passRings: RingDatum[] = [];
  const passLabels: { position: [number, number]; text: string }[] = [];
  for (const p of opts.passes ?? []) {
    const sat = sats.find((s) => s.key === p.satKey);
    if (!sat) continue;
    for (const ring of swathForPass(sat, p, 4)) passRings.push({ sat, ring });
    const at = subpoint(sat, p.tca);
    if (at) passLabels.push({ position: [at.lon, at.lat], text: `${p.satName} ${timeIST(p.tca)}` });
  }

  const look = opts.nextLook ? nextLookParts(sats, opts.nextLook.pass, opts.nextLook.site) : null;
  if (look) {
    out.push(
      new PathLayer<{ path: [number, number][] }, AnchorProps>({
        id: `${id}:look-track`,
        beforeId: DATA_ANCHOR,
        data: look.track.map((path) => ({ path })),
        visible,
        pickable: false,
        widthUnits: 'pixels',
        getPath: (d) => d.path,
        getColor: SATELLITE_STYLE.lookTrack,
        getWidth: SATELLITE_STYLE.lookTrackPx,
      }),
      new PathLayer<{ path: [number, number][] }, AnchorProps>({
        id: `${id}:look-scan`,
        beforeId: DATA_ANCHOR,
        data: look.scan.map((path) => ({ path })),
        visible,
        pickable: false,
        widthUnits: 'pixels',
        getPath: (d) => d.path,
        getColor: SATELLITE_STYLE.lookLine,
        getWidth: SATELLITE_STYLE.lookLinePx,
      }),
      new ScatterplotLayer<{ position: [number, number] }, AnchorProps>({
        id: `${id}:look-points`,
        beforeId: DATA_ANCHOR,
        data: [{ position: look.site }, { position: look.overhead }],
        visible,
        pickable: false,
        radiusUnits: 'pixels',
        lineWidthUnits: 'pixels',
        filled: true,
        stroked: true,
        getPosition: (d) => d.position,
        getRadius: 3.5,
        getFillColor: SATELLITE_STYLE.dotStroke,
        getLineColor: SATELLITE_STYLE.lookLine,
        getLineWidth: 1.5,
      }),
    );
    if ((opts.labels ?? true) && opts.nextLook?.label !== false) {
      out.push(
        new TextLayer<{ position: [number, number]; text: string }, AnchorProps>({
          // written along the scan line, just above it, and over the basemap's own labels (a town
          // name must not cover the one label that explains the line)
          id: `${id}:look-label`,
          beforeId: TOP_ANCHOR,
          data: [{ position: look.labelAt, text: look.label }],
          visible,
          pickable: false,
          getPosition: (d) => d.position,
          getText: (d) => d.text,
          getAngle: look.labelAngle,
          getPixelOffset: [-4 * Math.sin((look.labelAngle * Math.PI) / 180), -4 * Math.cos((look.labelAngle * Math.PI) / 180)],
          getSize: 12,
          sizeUnits: 'pixels',
          getColor: SATELLITE_STYLE.label,
          getTextAnchor: 'middle',
          getAlignmentBaseline: 'bottom',
          fontFamily: mapLabelFont(),
          fontWeight: 500,
          fontSettings: { sdf: true },
          outlineWidth: 3,
          outlineColor: SATELLITE_STYLE.dotStroke,
        }),
      );
    }
  }

  if (passRings.length) {
    out.push(
      new PolygonLayer<RingDatum, AnchorProps>({
        id: `${id}:passes`,
        beforeId: DATA_ANCHOR,
        data: passRings,
        visible,
        pickable: false,
        filled: true,
        stroked: true,
        lineWidthUnits: 'pixels',
        getPolygon: (d) => d.ring,
        getFillColor: SATELLITE_STYLE.passFill,
        getLineColor: SATELLITE_STYLE.passEdge,
        getLineWidth: 1,
      }),
    );
  }
  if (ringData.length) {
    out.push(
      new PolygonLayer<RingDatum, AnchorProps>({
        id: `${id}:swaths`,
        beforeId: DATA_ANCHOR,
        data: ringData,
        visible,
        pickable: false,
        filled: true,
        stroked: true,
        lineWidthUnits: 'pixels',
        getPolygon: (d) => d.ring,
        getFillColor: SATELLITE_STYLE.swathFill,
        getLineColor: SATELLITE_STYLE.swathEdge,
        getLineWidth: 1,
      }),
    );
  }
  if (opts.tracks ?? true) {
    const tracks: TrackDatum[] = chosen.flatMap((sat) => groundTrack(sat, now).map((path) => ({ sat, path })));
    out.push(
      new PathLayer<TrackDatum, AnchorProps>({
        id: `${id}:tracks`,
        beforeId: DATA_ANCHOR,
        data: tracks,
        visible,
        pickable: false,
        widthUnits: 'pixels',
        getPath: (d) => d.path,
        getColor: (d) => (isFire(d.sat) ? SATELLITE_STYLE.track : SATELLITE_STYLE.trackImaging),
        getWidth: SATELLITE_STYLE.trackPx,
      }),
    );
  }
  const here = positions(chosen, now);
  out.push(
    new ScatterplotLayer<(typeof here)[number], AnchorProps>({
      id: `${id}:positions`,
      beforeId: DATA_ANCHOR,
      data: here,
      visible,
      pickable: true,
      radiusUnits: 'pixels',
      lineWidthUnits: 'pixels',
      stroked: true,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: SATELLITE_STYLE.dotPx,
      getFillColor: SATELLITE_STYLE.dot,
      getLineColor: SATELLITE_STYLE.dotStroke,
      getLineWidth: 1.5,
    }),
  );
  if (opts.labels ?? true) {
    const labels = [...here.map((d) => ({ position: [d.lon, d.lat] as [number, number], text: d.sat.name, offset: [8, 0] as [number, number] })), ...passLabels.map((l) => ({ ...l, offset: [0, -10] as [number, number] }))];
    out.push(
      new TextLayer<(typeof labels)[number], AnchorProps>({
        id: `${id}:labels`,
        beforeId: DATA_ANCHOR,
        data: labels,
        visible,
        pickable: false,
        getPosition: (d) => d.position,
        getText: (d) => d.text,
        getPixelOffset: (d) => d.offset,
        getSize: 11,
        sizeUnits: 'pixels',
        getColor: SATELLITE_STYLE.label,
        getTextAnchor: 'start',
        getAlignmentBaseline: 'center',
        fontFamily: mapLabelFont(),
        fontWeight: 500,
        fontSettings: { sdf: true },
        outlineWidth: 3,
        outlineColor: SATELLITE_STYLE.dotStroke,
      }),
    );
  }
  return out;
}

export function describeSatellite(d: { sat: Satellite; heightKm: number }): string {
  return `${d.sat.name} (${d.sat.instrument})\n${Math.round(d.heightKm)} km up, swath ${Math.round(d.sat.halfSwathKm * 2)} km`;
}
