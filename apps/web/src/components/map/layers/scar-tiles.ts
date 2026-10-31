import type { Layer } from '@deck.gl/core';
import { PolygonLayer } from '@deck.gl/layers';

import { centroidOf, type LonLat, type ScarProps } from '@/lib/api';
import { metricText, number, percent } from '@/lib/format';

import { HIGHLIGHT, SCAR_TILE_STYLE, scarTileStep } from '../palette';
import { DATA_ANCHOR, type AnchorProps } from '../types';
import type { ScarFeature } from './scars';

/**
 * Burn scars at region zoom. A field is 1-4 ha, a pixel there is about 0.4 km across, so 2,500
 * fields drawn one by one merge into a single blob. Below zoom 9 the map instead bins the fields it
 * has into hexagons of a fixed size on screen (about 25 px across, re-binned as the zoom changes):
 * each tile's size says how many hectares burnt in it (square root, so area reads as area; the top
 * tenth of tiles are drawn full size), its
 * colour says what share of the checked hectares no fire alert explains, from the grey of a burn
 * with an alert to the gold of a candidate unseen burn. Tiles whose fields are all still unchecked
 * against the alerts are outlines only, like those fields.
 *
 * Pure: the same features and cell size always give the same tiles.
 */

export type ScarTile = {
  id: string;
  /** Hexagon centre and corners, lon/lat, at full cell size and scaled by hectares. */
  center: LonLat;
  ring: LonLat[];
  drawn: LonLat[];
  scars: number;
  areaHa: number;
  unseenHa: number;
  alertedHa: number;
  uncheckedHa: number;
  /** Unseen hectares over checked hectares (unseen + with an alert); null when nothing is checked. */
  unseenShare: number | null;
  /** The cell's box [w, s, e, n]. */
  bounds: [number, number, number, number];
  /** The box of the scars' centroids inside it [w, s, e, n], for zooming in to them. */
  extent: [number, number, number, number];
  /** Set on a district's tile (the season by district, `districtTiles`); `extent` is then the
   *  district's own box. */
  district?: { name: string; state: string; fires: number | null };
};

const R = 6378137; // Web Mercator sphere, metres
const D2R = Math.PI / 180;
const toX = (lon: number) => lon * D2R * R;
const toY = (lat: number) => R * Math.log(Math.tan(Math.PI / 4 + (Math.max(-85, Math.min(85, lat)) * D2R) / 2));
const toLon = (x: number) => x / R / D2R;
const toLat = (y: number) => (2 * Math.atan(Math.exp(y / R)) - Math.PI / 2) / D2R;

/** Metres of Web Mercator per screen pixel at a zoom (MapLibre's 512 px tiles). */
export const mercatorMetresPerPixel = (zoom: number): number => (2 * Math.PI * R) / 512 / 2 ** zoom;

/** Pointy-top hexagon corners around (cx, cy) in Mercator metres, radius s. */
function corners(cx: number, cy: number, s: number): LonLat[] {
  const out: LonLat[] = [];
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 180) * (60 * i - 30);
    out.push([toLon(cx + s * Math.cos(a)), toLat(cy + s * Math.sin(a))]);
  }
  out.push(out[0]);
  return out;
}

/** Nearest hexagon (axial q, r) to a point, by cube rounding. */
function hexOf(x: number, y: number, s: number): [number, number] {
  const qf = ((Math.sqrt(3) / 3) * x - (1 / 3) * y) / s;
  const rf = ((2 / 3) * y) / s;
  const sf = -qf - rf;
  let q = Math.round(qf);
  let r = Math.round(rf);
  const sr = Math.round(sf);
  const dq = Math.abs(q - qf);
  const dr = Math.abs(r - rf);
  const ds = Math.abs(sr - sf);
  if (dq > dr && dq > ds) q = -r - sr;
  else if (dr > ds) r = -q - sr;
  return [q, r];
}

/** A polygon's first ring averaged, when the API sent no centroid. */
function ringCentroid(f: ScarFeature): LonLat | null {
  const g = f.geometry;
  const ring = g.type === 'Polygon' ? g.coordinates[0] : g.coordinates[0]?.[0];
  if (!ring?.length) return null;
  let x = 0;
  let y = 0;
  for (const p of ring) {
    x += p[0];
    y += p[1];
  }
  return [x / ring.length, y / ring.length];
}

/**
 * Bin scars into hexagons of radius `radiusPx` screen pixels at `zoom`. Each scar counts where its
 * centroid falls, with its full area (a field never straddles two 5 km cells by much).
 */
export function binScars(features: readonly ScarFeature[], zoom: number, radiusPx = SCAR_TILE_STYLE.radiusPx): ScarTile[] {
  const s = radiusPx * mercatorMetresPerPixel(zoom);
  const cells = new Map<string, Omit<ScarTile, 'ring' | 'drawn' | 'unseenShare' | 'bounds' | 'center' | 'id'> & { q: number; r: number }>();
  for (const f of features) {
    const p = f?.properties as ScarProps | undefined;
    if (!p) continue;
    const c = centroidOf(p.centroid) ?? ringCentroid(f);
    if (!c) continue;
    const [q, r] = hexOf(toX(c[0]), toY(c[1]), s);
    const key = `${q},${r}`;
    let cell = cells.get(key);
    if (!cell) {
      cell = { q, r, scars: 0, areaHa: 0, unseenHa: 0, alertedHa: 0, uncheckedHa: 0, extent: [c[0], c[1], c[0], c[1]] };
      cells.set(key, cell);
    }
    const ha = Number.isFinite(p.area_ha) ? p.area_ha : 0;
    cell.scars += 1;
    cell.extent = [Math.min(cell.extent[0], c[0]), Math.min(cell.extent[1], c[1]), Math.max(cell.extent[2], c[0]), Math.max(cell.extent[3], c[1])];
    cell.areaHa += ha;
    if (p.unseen === true) cell.unseenHa += ha;
    else if (p.unseen === false) cell.alertedHa += ha;
    else cell.uncheckedHa += ha;
  }
  // a full-size tile is the 90th percentile of tile area and above, so one exceptional cell does
  // not shrink every other tile to a speck; below it, size goes with the square root of hectares
  const areas = [...cells.values()].map((c) => c.areaHa).sort((a, b) => a - b);
  const max = areas.length ? areas[Math.min(areas.length - 1, Math.floor(areas.length * 0.9))] : 0;
  const out: ScarTile[] = [];
  for (const c of cells.values()) {
    const cx = s * (Math.sqrt(3) * c.q + (Math.sqrt(3) / 2) * c.r);
    const cy = s * (1.5 * c.r);
    const ring = corners(cx, cy, s);
    const k = max > 0 ? Math.min(1, Math.sqrt(c.areaHa / max)) : 1;
    const drawn = corners(cx, cy, s * SCAR_TILE_STYLE.gap * Math.max(SCAR_TILE_STYLE.minScale, k));
    const checked = c.unseenHa + c.alertedHa;
    const lons = ring.map((p) => p[0]);
    const lats = ring.map((p) => p[1]);
    out.push({
      id: `${c.q},${c.r}`,
      center: [toLon(cx), toLat(cy)],
      ring,
      drawn,
      scars: c.scars,
      areaHa: c.areaHa,
      unseenHa: c.unseenHa,
      alertedHa: c.alertedHa,
      uncheckedHa: c.uncheckedHa,
      unseenShare: checked > 0 ? c.unseenHa / checked : null,
      bounds: [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)],
      extent: c.extent,
    });
  }
  // largest last, so a big tile's edge is never hidden under a small neighbour
  return out.sort((a, b) => a.areaHa - b.areaHa);
}

export type ScarTilesOptions = { id?: string; visible?: boolean; pickable?: boolean };

/** The hexagon tiles as one deck.gl layer (id `scars:tiles`, so the registry credits Sentinel-2). */
export function scarTilesLayer(tiles: readonly ScarTile[], opts: ScarTilesOptions = {}): Layer[] {
  if (!tiles.length) return [];
  return [
    new PolygonLayer<ScarTile, AnchorProps>({
      id: `${opts.id ?? 'scars'}:tiles`,
      beforeId: DATA_ANCHOR,
      data: tiles as ScarTile[],
      visible: opts.visible ?? true,
      pickable: opts.pickable ?? true,
      autoHighlight: true,
      highlightColor: HIGHLIGHT,
      filled: true,
      stroked: true,
      lineWidthUnits: 'pixels',
      getPolygon: (t) => t.drawn,
      getFillColor: (t) => (t.unseenShare == null ? SCAR_TILE_STYLE.uncheckedFill : SCAR_TILE_STYLE.steps[scarTileStep(t.unseenShare)]),
      getLineColor: (t) => (t.unseenShare == null ? SCAR_TILE_STYLE.uncheckedStroke : SCAR_TILE_STYLE.stroke),
      getLineWidth: (t) => (t.unseenShare == null ? 1 : 0),
    }),
  ];
}

/** Tooltip text for one tile. "candidate unseen burn" is the API's own label for these scars;
 *  `precision` is the audit line the page read from the API (it is never typed in here). */
export function describeScarTile(t: ScarTile, precision?: string | null): string {
  const checked = t.unseenHa + t.alertedHa;
  return [
    `${number(t.scars)} burn scar${t.scars === 1 ? '' : 's'}, ${metricText('area_ha', t.areaHa)}`,
    t.unseenShare == null
      ? 'Not yet checked against fire alerts'
      : `${metricText('area_ha', t.unseenHa)} candidate unseen burn (${percent(t.unseenShare)} of the ${metricText('area_ha', checked)} checked)`,
    t.unseenHa > 0 && precision ? precision : null,
    t.alertedHa > 0 && t.unseenShare != null ? `${metricText('area_ha', t.alertedHa)} with a fire alert` : null,
    t.uncheckedHa > 0 && t.unseenShare != null ? `${metricText('area_ha', t.uncheckedHa)} not yet checked` : null,
    'Click to see its fields',
  ]
    .filter(Boolean)
    .join('\n');
}

// ---------------------------------------------------------------------------------------------
// the season by district (region zoom)

/** One row of /api/scars/summary `by_district` (the fields the tiles read). */
export type DistrictSeasonRow = {
  district: string;
  state: string;
  area_ha: number;
  clusters: number;
  unseen_area_ha: number;
  unseen_share: number | null;
  fire_detections?: number | null;
};
/** A Survey of India district polygon from /api/geo/boundaries?level=district. */
export type DistrictShape = {
  properties?: { name?: string | null; state?: string | null; aliases?: readonly string[] | null } | null;
  geometry?: { type: string; coordinates: unknown } | null;
};

type Ring = [number, number][];

/** Area-weighted centroid of a district's largest outer ring, and the box of all its rings (planar
 *  in degrees: close enough for a mark placed on a district tens of km across). */
function shapeCentre(g: DistrictShape['geometry']): { c: LonLat; box: [number, number, number, number] } | null {
  if (!g) return null;
  const outers: Ring[] = g.type === 'Polygon' ? [(g.coordinates as Ring[])[0]] : g.type === 'MultiPolygon' ? (g.coordinates as Ring[][]).map((p) => p[0]) : [];
  let best: { a: number; x: number; y: number } | null = null;
  const box: [number, number, number, number] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const ring of outers) {
    if (!ring || ring.length < 4) continue;
    let a = 0;
    let x = 0;
    let y = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      const [x0, y0] = ring[i];
      const [x1, y1] = ring[i + 1];
      const k = x0 * y1 - x1 * y0;
      a += k;
      x += (x0 + x1) * k;
      y += (y0 + y1) * k;
      box[0] = Math.min(box[0], x0);
      box[1] = Math.min(box[1], y0);
      box[2] = Math.max(box[2], x0);
      box[3] = Math.max(box[3], y0);
    }
    if (a !== 0 && (!best || Math.abs(a) > Math.abs(best.a))) best = { a, x: x / (3 * a), y: y / (3 * a) };
  }
  return best && Number.isFinite(box[0]) ? { c: [best.x, best.y], box } : null;
}

const norm = (s: string | null | undefined) => (s ?? '').trim().toLowerCase();

/**
 * The season's burn scars by district, one hexagon on each district (at its Survey of India
 * polygon's centroid): size by the square root of the hectares burnt (the largest district full
 * size), colour by the share of its checked hectares that is candidate unseen burn, in the same
 * steps as the field tiles. Every figure is the summary's own row; a district with no polygon to
 * place it on is left out, and so is one with no hectares.
 */
export function districtTiles(rows: readonly DistrictSeasonRow[], shapes: readonly DistrictShape[], zoom: number): ScarTile[] {
  const where = new Map<string, { c: LonLat; box: [number, number, number, number] }>();
  for (const f of shapes) {
    const p = f.properties;
    const at = shapeCentre(f.geometry);
    if (!p || !at) continue;
    for (const n of [p.name, ...(p.aliases ?? [])]) if (n) where.set(`${norm(n)}|${norm(p.state)}`, at);
  }
  const placed = rows.filter((r) => r.area_ha > 0 && where.has(`${norm(r.district)}|${norm(r.state)}`));
  const max = Math.max(0, ...placed.map((r) => r.area_ha));
  const mpp = mercatorMetresPerPixel(zoom);
  const out: ScarTile[] = [];
  for (const r of placed) {
    const at = where.get(`${norm(r.district)}|${norm(r.state)}`);
    if (!at) continue;
    const [lon, lat] = at.c;
    const cx = toX(lon);
    const cy = toY(lat);
    // km of ground in Mercator metres at this latitude, with a floor in screen pixels
    const full = Math.max((SCAR_TILE_STYLE.districtRadiusKm * 1000) / Math.cos(lat * D2R), SCAR_TILE_STYLE.districtMinPx * mpp);
    const k = max > 0 ? Math.max(SCAR_TILE_STYLE.districtMinScale, Math.sqrt(r.area_ha / max)) : 1;
    const share = r.unseen_share != null && Number.isFinite(r.unseen_share) ? r.unseen_share : null;
    // checked hectares from the summary's own share (unseen / checked); alerted is the rest of them
    const checked = share != null && share > 0 ? r.unseen_area_ha / share : null;
    out.push({
      id: `d:${r.state}:${r.district}`,
      center: [lon, lat],
      ring: corners(cx, cy, full),
      drawn: corners(cx, cy, full * SCAR_TILE_STYLE.gap * k),
      scars: r.clusters,
      areaHa: r.area_ha,
      unseenHa: r.unseen_area_ha,
      alertedHa: checked != null ? Math.max(0, checked - r.unseen_area_ha) : share === 0 ? r.area_ha : 0,
      uncheckedHa: checked != null ? Math.max(0, r.area_ha - checked) : 0,
      unseenShare: share,
      bounds: at.box,
      extent: at.box,
      district: { name: r.district, state: r.state, fires: r.fire_detections ?? null },
    });
  }
  return out.sort((a, b) => a.areaHa - b.areaHa);
}

/** Tooltip text for a district's tile; `since` is the summary's season start (already formatted),
 *  `precision` the audit line the page read from the API. */
export function describeDistrictTile(t: ScarTile, since: string | null, precision?: string | null): string {
  const d = t.district;
  return [
    `${d?.name ?? 'District'} district${since ? `, since ${since}` : ''}`,
    `${number(t.scars)} burn scar${t.scars === 1 ? '' : 's'}, ${metricText('area_ha', t.areaHa)}`,
    t.unseenShare == null ? 'Not yet checked against fire alerts' : `${metricText('area_ha', t.unseenHa)} candidate unseen burn (${percent(t.unseenShare)} of the hectares checked)`,
    t.unseenHa > 0 && precision ? precision : null,
    d?.fires != null ? `${number(d.fires)} FIRMS fire alerts in the district` : null,
    'Click to zoom in to this district',
  ]
    .filter(Boolean)
    .join('\n');
}
