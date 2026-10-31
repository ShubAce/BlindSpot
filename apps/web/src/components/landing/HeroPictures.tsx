import { absoluteApiUrl } from '@/lib/api';
import { dateIST, metricText, number } from '@/lib/format';

import { Crossfade, type Frame } from './Crossfade';
import { getFeatured } from './data';
import { s } from './styles';
import { Pic } from './Pic';
import { sentinelName } from './words';

/**
 * The two full-bleed pictures the rail crosses: the same Punjab fields on four Sentinel-2 passes
 * (GET /api/imagery/featured, crossfading), then the newest pass in short-wave infrared. The place,
 * the dates and the scar total in the captions are the API's.
 */
export async function HeroPictures() {
  const r = await getFeatured();
  if (!r.ok) {
    return (
      <div className={s.picMissing}>
        <p>{`The Sentinel-2 pictures of the featured fields did not load. ${r.error.message}`}</p>
      </div>
    );
  }
  const { aoi, frames: raw = [], reason } = r.data;
  const district = aoi?.district ?? null;
  const placeShort = aoi?.name?.replace(/,.*$/, '').replace(/^Near\s+/i, '') ?? null;
  const frames: Frame[] = raw
    .map((f) => ({ f, src: absoluteApiUrl(f.truecolor_url) }))
    .filter((x): x is { f: (typeof raw)[number]; src: string } => !!x.src)
    .map(({ f, src }) => ({
      src,
      width: f.width,
      height: f.height,
      date: f.date,
      dateLabel: dateIST(f.datetime ?? f.date),
      satellite: sentinelName(f.platform ?? f.item_id),
      alt: `Sentinel-2 photograph of fields near ${placeShort ?? 'the featured village'}${district ? `, ${district} district` : ''}, Punjab, on ${dateIST(f.datetime ?? f.date, { year: true })}. Black patches are burnt fields.`,
    }));

  if (!frames.length) {
    return (
      <div className={s.picMissing}>
        <p>{reason ? `No clear Sentinel-2 passes of the featured fields yet: ${reason}` : 'No clear Sentinel-2 passes of the featured fields yet.'}</p>
      </div>
    );
  }

  const newest = raw[raw.length - 1];
  const swir = absoluteApiUrl(newest?.swir_url);
  const place = aoi?.name ? `${aoi.name}${aoi.state ? `, ${aoi.state}` : ''}` : 'The featured fields';
  const scarLine =
    aoi?.chosen_value != null && aoi.chosen_unit === 'ha'
      ? `${metricText('area_ha', aoi.chosen_value)} of burn scars here${aoi.chosen_count != null ? ` in ${number(aoi.chosen_count)} fields` : ''}${aoi.chosen_since ? ` since ${dateIST(aoi.chosen_since)}` : ''}.`
      : null;

  return (
    <>
      <div className={s.picOne}>
        <Crossfade frames={frames} place={`${place}. Sentinel-2, true colour.`} />
      </div>
      <div className={s.picTwo}>
        {swir && newest ? (
          <Pic
            src={swir}
            width={newest.width}
            height={newest.height}
            alt={`The same fields on ${dateIST(newest.datetime ?? newest.date, { year: true })} in Sentinel-2's short-wave infrared bands.`}
            className={s.cover}
            failText="The short-wave infrared picture did not load from the API."
          />
        ) : null}
        <p className={s.picTwoCaption}>
          {`The same fields on ${dateIST(newest?.datetime ?? newest?.date)}, short-wave infrared${sentinelName(newest?.platform ?? newest?.item_id) ? `, ${sentinelName(newest?.platform ?? newest?.item_id)}` : ''}.`}
          {scarLine ? ` ${scarLine}` : ''}
        </p>
      </div>
    </>
  );
}
