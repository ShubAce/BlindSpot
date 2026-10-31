import { LoadFailed } from '@/components/pwa/LoadFailed';
import { absoluteApiUrl } from '@/lib/api';
import { dateIST, istDayKey } from '@/lib/format';

import { Crossfade, type Frame, type FrameMarks } from './Crossfade';
import { getFeatured, getFrameScars } from './data';
import { s } from './styles';
import type { FrameScars, ScarPolygon } from './types';
import { sentinelName } from './words';

/**
 * The hero's picture: the same Punjab fields on four Sentinel-2 passes (GET /api/imagery/featured),
 * crossfading, uncropped, with every burn scar inside the frame's box outlined on it
 * (GET /api/scars?bbox=): gold where no fire alert explains it (a candidate unseen burn), white where
 * FIRMS saw a fire. Each outline appears from the first pass whose date is on or after the end of
 * that scar's burn window, so the fields burn as the passes go by, and the caption counts them.
 */
export async function HeroPictures() {
  const r = await getFeatured();
  if (!r.ok) {
    return (
      <div className={s.picMissing}>
        <p data-load-failed="">{`The Sentinel-2 pictures of the featured fields did not load. ${r.error.message}`}</p>
      </div>
    );
  }
  const { aoi, frames: raw = [], reason } = r.data;
  const district = aoi?.district ?? null;
  const placeShort = aoi?.name?.replace(/,.*$/, '').replace(/^Near\s+/i, '') ?? null;
  const usable = raw.map((f) => ({ f, src: absoluteApiUrl(f.truecolor_url) })).filter((x): x is { f: (typeof raw)[number]; src: string } => !!x.src);
  const frames: Frame[] = usable.map(({ f, src }) => ({
    src,
    width: f.width,
    height: f.height,
    date: f.date,
    dateLabel: dateIST(f.datetime ?? f.date),
    satellite: sentinelName(f.platform ?? f.item_id),
    alt: `Sentinel-2 photograph of fields near ${placeShort ?? 'the featured village'}${district ? `, ${district} district` : ''}, Punjab, on ${dateIST(f.datetime ?? f.date, { year: true })}, with the burn scars mapped so far outlined.`,
  }));

  if (!frames.length) {
    return (
      <div className={s.picMissing}>
        <p>{reason ? `No clear Sentinel-2 passes of the featured fields yet: ${reason}` : 'No clear Sentinel-2 passes of the featured fields yet.'}</p>
      </div>
    );
  }

  const place = aoi?.name ? `${aoi.name}${aoi.state ? `, ${aoi.state}` : ''}` : 'The featured fields';
  const bbox = aoi?.bbox && aoi.bbox.length === 4 ? aoi.bbox : null;
  const scars = bbox ? await getFrameScars(bbox) : null;
  const marks = bbox && scars?.ok ? frameMarks(scars.data, bbox, usable.map(({ f }) => istDayKey(f.datetime ?? f.date)), frames[0].width, frames[0].height) : null;
  const marksNote = !bbox
    ? 'The featured window came without its box, so its burn scars cannot be outlined.'
    : scars && !scars.ok
      ? `The burn scars for this frame did not load. ${scars.error.message}`
      : null;

  return (
    <>
      <Crossfade frames={frames} place={`${place}. Sentinel-2, true colour.`} marks={marks} marksNote={marksNote} />
      {/* the pictures (a client component) say the outlines failed; the mark is for the offline worker */}
      {scars && !scars.ok ? <LoadFailed /> : null}
    </>
  );
}

/** Each frame's new outlines (gold and white paths in the picture's pixels) and its running counts. */
function frameMarks(data: FrameScars, bbox: number[], frameDays: string[], w: number, h: number): FrameMarks {
  const [x0, y0, x1, y1] = bbox;
  const sx = w / (x1 - x0);
  const sy = h / (y1 - y0);
  const ring = (pts: number[][]) => {
    let d = '';
    let last = '';
    for (const [lon, lat] of pts) {
      const p = `${Math.round((lon - x0) * sx)} ${Math.round((y1 - lat) * sy)}`;
      if (p === last) continue;
      d += (d ? 'L' : 'M') + p;
      last = p;
    }
    return d ? `${d}Z` : '';
  };
  const pathOf = (g: ScarPolygon | null) => {
    if (!g) return '';
    const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
    return polys.map((rings) => rings.map(ring).join('')).join('');
  };

  const groups = frameDays.map(() => ({ unseen: '', seen: '' }));
  const tally = frameDays.map(() => ({ unseen: 0, seen: 0, unchecked: 0, ha: 0 }));
  for (const f of data.features?.features ?? []) {
    const p = f.properties;
    const end = istDayKey(p.burn_before);
    const k = frameDays.findIndex((day) => end && end <= day);
    if (k < 0) continue; // burnt after the newest frame
    for (let i = k; i < frameDays.length; i++) {
      tally[i].ha += p.area_ha ?? 0;
      if (p.unseen === true) tally[i].unseen += 1;
      else if (p.unseen === false) tally[i].seen += 1;
      else tally[i].unchecked += 1;
    }
    if (p.unseen == null) continue; // not yet checked against the fire alerts: no colour to give it
    const d = pathOf(f.geometry);
    if (p.unseen) groups[k].unseen += d;
    else groups[k].seen += d;
  }
  return {
    groups,
    tally,
    since: data.window?.first_pass ?? null,
    truncated: !!data.truncated,
  };
}
