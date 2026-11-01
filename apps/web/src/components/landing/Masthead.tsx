import Link from 'next/link';

import { s } from './styles';


/**
 * The landing's title row, just under the site bar: the h1 (the name with what it does), the one
 * supporting sentence, and the page's one action, "Open mission control". The h1 is the first screen's
 * largest type (two lines over the picture on desktop and on a phone), so it reads as the page's
 * heading before the clock's hub figure; the gold button sits under the sentence on desktop and on a
 * phone, beside it on a tablet (landing.css, title row). The site bar is the same on every page and
 * carries places only, so the landing's call to action lives here, on the landing. It plays the
 * landing's one view transition into mission control ('to-live', app/page.tsx).
 *
 * The sentence is the thesis in words, no figure: the figures sit just below it (the share of burnt
 * area with no fire alert, and the clock of who looks when), each read from the API.
 */
export function Masthead() {
  return (
    <div className={s.rail}>
      <div className={s.railInner}>
        <h1 className={s.railTitle}>BlindSpot looks between the passes</h1>
        <div className={s.railLead}>
          <p className={s.railSentence}>Fields are lit after the fire satellites pass over, so their fire counts miss most of the burning.</p>
          <Link href="/live" className={s.railAction} transitionTypes={['to-live']}>
            Open mission control
          </Link>
        </div>
      </div>
    </div>
  );
}
