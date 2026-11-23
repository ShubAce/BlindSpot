/**
 * The marks on the case file's pictures, in the pictures' own square (0..VIEW on both axes, north
 * up): the case's outline, and every other stored scar inside the same box (GET /api/scars/{id},
 * `other_scars`), so a reader sees which dark fields the model mapped and not only this one.
 *
 * Pure functions. CaseScreen (a server component) turns the other scars into short path strings
 * here, so the browser receives a few characters a corner instead of their coordinates (about 4
 * against 21), and the sentence under the pictures takes its numbers from the API's totals.
 */

import { dateIST, dateRangeIST, metricText, NBSP, number, plural } from '@/lib/format';

import type { CaseProps, OtherScar, OtherScars, OtherScarTotals } from './types';

export type BBox = [number, number, number, number];
export const VIEW = 1000;

type Outline = CaseProps['geometry'];

export function ringsOf(g: Outline): number[][][] {
  if (!g) return [];
  return g.type === 'Polygon' ? g.coordinates : g.coordinates.flat();
}

/** The case's own outline, to a tenth of a unit. */
export function pathFor(g: Outline, bbox: BBox): string {
  const [x0, y0, x1, y1] = bbox;
  const px = (lon: number) => ((lon - x0) / (x1 - x0)) * VIEW;
  const py = (lat: number) => ((y1 - lat) / (y1 - y0)) * VIEW;
  return ringsOf(g)
    .map((ring) => ring.map((p, i) => `${i ? 'L' : 'M'}${px(p[0]).toFixed(1)} ${py(p[1]).toFixed(1)}`).join('') + 'Z')
    .join('');
}

/** Another scar's outline in whole units (half a picture pixel: finer than the API's one-pixel
 *  simplification), each corner relative to the last and straight runs as h or v. Scars are traced
 *  from pixels, so most corners are axis-aligned steps of a few characters. */
export function compactPath(g: Outline, bbox: BBox): string {
  const [x0, y0, x1, y1] = bbox;
  const sx = VIEW / (x1 - x0);
  const sy = VIEW / (y1 - y0);
  let d = '';
  for (const ring of ringsOf(g)) {
    const last = ring[ring.length - 1];
    const n = ring.length > 1 && ring[0][0] === last[0] && ring[0][1] === last[1] ? ring.length - 1 : ring.length;
    let x = 0;
    let y = 0;
    for (let i = 0; i < n; i++) {
      const nx = Math.round((ring[i][0] - x0) * sx);
      const ny = Math.round((y1 - ring[i][1]) * sy);
      if (i === 0) d += `M${nx} ${ny}`;
      else if (nx === x && ny === y) continue;
      else if (ny === y) d += `h${nx - x}`;
      else if (nx === x) d += `v${ny - y}`;
      else d += `l${nx - x} ${ny - y}`;
      x = nx;
      y = ny;
    }
    if (n) d += 'z';
  }
  return d;
}

/** A field-sized area keeps two decimals (0.72 ha), as the case's own figure does. */
const areaText = (ha: number) => (ha < 10 ? `${number(ha, { decimals: 2 })}${NBSP}ha` : metricText('area_ha', ha));

export type OtherMark = { id: number; rel: 'same' | 'earlier'; d: string; title: string };

const drawn = (s: OtherScar): s is OtherScar & { relation: 'same' | 'earlier' } => s.relation === 'same' || s.relation === 'earlier';

/** The other scars the pictures outline, in drawing order: the larger first, whatever their kind,
 *  so a small field lying on a large one stays on top and clickable. Scars first seen after the
 *  pictures are not drawn (the API counts them without listing them): they burnt in neither. */
export function otherMarks(o: OtherScars | null | undefined, bbox: BBox | null): OtherMark[] {
  if (!o?.scars || !bbox) return [];
  return o.scars
    .filter(drawn)
    .filter((s) => s.outline)
    .sort((a, b) => b.area_ha - a.area_ha || a.id - b.id)
    .map((s) => ({
      id: s.id,
      rel: s.relation,
      d: compactPath(s.outline, bbox),
      title: `Case ${s.id}: ${areaText(s.area_ha)}, burnt ${dateRangeIST(s.burn_after, s.burn_before)}`,
    }))
    .filter((m) => m.d);
}

/** The case's two passes: the before picture's and the after picture's. */
export type PictureWindow = { after: string; before: string };

/** The key's words for the two kinds, with the case's own dates: what the rule knows of a scar is
 *  when it was first seen burnt. */
export const othersLabels = (w: PictureWindow) => ({
  same: `Other scars, first seen burnt ${dateRangeIST(w.after, w.before)}`,
  earlier: `Other scars, burnt by ${dateIST(w.after)}`,
});

const NONE: OtherScarTotals = { scars: 0, area_ha: 0 };

/** One plain sentence under the pictures, every number from the API's totals and the dates from
 *  the case's two passes. "First seen burnt between" and "had burnt by" say what the rule knows: a
 *  scar's window runs from the last clear pass that saw it unburnt to the first that saw it burnt.
 *  The areas are the part of the scars inside the picture (`area_in_box_ha`): many reach past its
 *  edge, so their whole area would overstate what the picture shows. */
export function othersSentence(o: OtherScars | null | undefined, marks: number, w: PictureWindow): string | null {
  if (!o?.totals) return null;
  const same = o.totals.same ?? NONE;
  const earlier = o.totals.earlier ?? NONE;
  const later = o.totals.later?.scars ?? 0;
  const shown = same.scars + earlier.scars;
  const cut = o.min_confidence != null ? ` with confidence ${number(o.min_confidence, { decimals: 1 })} or more` : '';
  if (!shown) {
    // later scars only: none had burnt by the after picture, so none is drawn
    return later
      ? `No other burn scar${cut} had been seen burnt in this picture by ${dateIST(w.before)}. ${number(later)} ${later === 1 ? 'other, first seen burnt here after that day, is' : 'others, first seen burnt here after that day, are'} not drawn.`
      : `No other burn scar${cut} is mapped in this picture.`;
  }
  // no area at all rather than a scar's whole area where the part inside the picture is unknown
  const inside = (t: OtherScarTotals, where: string) => (t.area_in_box_ha != null ? ` (${areaText(t.area_in_box_ha)} ${where})` : '');
  const parts: string[] = [];
  if (same.scars) {
    parts.push(`${plural(same.scars, 'other burn scar', 'other burn scars')} first seen burnt between ${dateIST(w.after)} and ${dateIST(w.before)}${inside(same, 'inside the picture')}`);
  }
  if (earlier.scars) {
    const what = same.scars ? number(earlier.scars) : plural(earlier.scars, 'other burn scar', 'other burn scars');
    parts.push(`${what} that had burnt by ${dateIST(w.after)}${inside(earlier, same.scars ? 'inside it' : 'inside the picture')}`);
  }
  let text = `Also mapped in this picture: ${parts.join(' and ')}${cut ? `, ${shown === 1 ? '' : 'all '}${cut.trim()}, as on the live map` : ''}.`;
  if (marks < shown) text += ` Only the ${number(marks)} largest are outlined.`;
  if (later) text += ` ${number(later)} more, first seen burnt after ${dateIST(w.before)}, ${later === 1 ? 'is' : 'are'} not drawn.`;
  return text;
}
