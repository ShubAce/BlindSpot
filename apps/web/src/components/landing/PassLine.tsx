'use client';

import { REFERENCE_SITE } from '@/lib/aoi';
import { timeIST } from '@/lib/format';
import { useLiveStatus } from '@/lib/use-orbits';

import { countdown } from './words';

/**
 * The page's live thing: who is watching Punjab now, from the satellites' orbital elements, computed
 * in the browser every second (lib/orbits.ts; checked against the API's own pass list to under a
 * second). Without script, or before the elements arrive, one plain line says where the times come from;
 * no time is typed in its place.
 */
export function PassLine({ className }: { className?: string }) {
  const { status, now, error } = useLiveStatus(REFERENCE_SITE, 1_000);

  let text: string;
  if (status && now) {
    const w = status.watching[0];
    if (w) {
      text = `${w.name} is over Punjab now. It leaves in ${countdown(w.until.getTime() - now.getTime())}.`;
    } else {
      const last = status.last ? `${status.last.name} passed at ${timeIST(status.last.at)} IST` : null;
      const next = status.next ? `${status.next.name} looks next at ${timeIST(status.next.at)}, in ${countdown(status.next.at.getTime() - now.getTime())}` : null;
      text = `None is overhead now.${last || next ? ` ${[last, next].filter(Boolean).join('; ')}.` : ''}`;
    }
  } else if (error) {
    text = 'Pass times are unavailable right now: the satellites’ orbital elements did not load.';
  } else {
    text = 'Pass times are worked out in your browser from the satellites’ orbital elements.';
  }

  return (
    <p className={className} aria-live="off" data-live={status ? 'on' : 'off'}>
      {text}
    </p>
  );
}
