'use client';

import { PATHS } from '@/lib/api';
import { REFERENCE_SITE } from '@/lib/aoi';
import { number, percent, timeIST } from '@/lib/format';
import { CADENCE, useApi } from '@/lib/live';
import { useLiveStatus } from '@/lib/use-orbits';

import { s } from './styles';
import type { Headline } from './types';
import { countdown, span } from './words';

/**
 * Section 1's statement, recomputed every second: the pass clock from the orbital elements (browser)
 * and the day's counts from GET /api/stats/headline (polled every minute, first paint from the server).
 * Every figure is bound; a missing block drops its sentence and the note says why.
 */
export function IntroLive({ initial, serverError }: { initial: Headline | null; serverError: string | null }) {
  const head = useApi<Headline>(PATHS.headline, undefined, { refreshInterval: CADENCE.headline, fallbackData: initial ?? undefined });
  const { status, now } = useLiveStatus(REFERENCE_SITE, 1_000);
  const h = head.data;

  const fires = h?.fires_24h?.available ? h.fires_24h : null;
  const geo = h?.gk2a_evening?.available ? h.gk2a_evening : null;

  // The pass sentence: the browser clock when it runs, else the server's last/next (no countdown).
  let passes: string;
  let during = false;
  if (status && now) {
    const w = status.watching[0];
    if (w) {
      during = true;
      passes = `${w.name} is over Punjab right now and leaves in ${countdown(w.until.getTime() - now.getTime())}.`;
    } else {
      const last = status.last ? ` ${status.last.name} last looked at ${timeIST(status.last.at)} IST.` : '';
      const next = status.next ? ` The next look is ${status.next.name} at ${timeIST(status.next.at)}, in ${countdown(status.next.at.getTime() - now.getTime())}.` : '';
      passes = `Right now no fire satellite can see Punjab.${last}${next}`;
    }
  } else if (h?.passes?.available && (h.passes.last || h.passes.next)) {
    const last = h.passes.last ? ` ${h.passes.last.satellite} last looked at ${timeIST(h.passes.last.at_utc)} IST.` : '';
    const next = h.passes.next ? ` The next look is ${h.passes.next.satellite} at ${timeIST(h.passes.next.at_utc)}.` : '';
    passes = `${h.passes.watching?.length ? `${h.passes.watching[0].satellite} is over Punjab now.` : 'No fire satellite can see Punjab right now.'}${last}${next}`;
  } else {
    passes = 'The satellite pass times have not loaded, so this page cannot say who is watching Punjab right now.';
  }

  const counted =
    fires?.count != null
      ? `In the 24 hours to ${fires.window_ist?.to ? fires.window_ist.to.slice(11, 16) : timeIST(h?.as_of)} IST the fire satellites recorded ${number(fires.count)} fire detections${fires.region ? ` in ${fires.region.replace(/\s*\(.*\)\s*$/, '').replace(/, ([^,]+)$/, ' and $1')}` : ''}.`
      : '';
  const after = during ? ' Fields lit after it leaves are not counted until the scars show.' : '';
  const evening =
    geo?.share_outside_polar_windows != null && geo.days
      ? ` The geostationary GK-2A satellite, which scans every ten minutes, put ${percent(geo.share_outside_polar_windows)} of the last ${number(geo.days)} days’ fire activity outside those looks${geo.peak_ist ? `, peaking at ${span(geo.peak_ist)} IST` : ''}.`
      : '';

  return (
    <>
      <p className={s.statement}>
        {passes}
        {after}
      </p>
      {counted || evening ? (
        <p className={s.introBody}>
          {counted}
          {evening}
        </p>
      ) : null}
      {!h && (serverError || head.error) ? <p className={s.honest}>{`Today’s counts did not load. ${serverError ?? head.error?.message ?? ''}`.trim()}</p> : null}
    </>
  );
}
