'use client';

import { useState, type MouseEvent, type ReactNode } from 'react';

import { LANGS } from '@/lib/langs';

import { csvFileName, csvHref } from './read';
import type { BriefWindow, CsvLang } from './types';

/**
 * The brief's CSV links, on a district (CsvButtons) and on each row of the index (CsvRowLink).
 *
 * Each is still a plain link to the API's attachment, so it works before JavaScript, and a middle
 * click or "save link as" stays the browser's own. A plain click downloads it with fetch() instead:
 * the API is another origin, so following the link is a navigation the offline worker never sees,
 * and with no connection the browser's network error page replaced the brief. Fetched, the brief
 * stays on screen and a note under the link says why nothing downloaded. A CSV this browser fetched
 * before also comes from the worker's saved copy (public/sw.js, the api store, network first), and
 * the page's offline notice says how old it is. The file keeps its language in its name: a blob of
 * this page's own origin honours `download`, where a cross-origin link kept the API's name, which is
 * the same for all three languages.
 */

type Outcome = 'idle' | 'busy' | 'offline' | 'error';

/** How long a click waits for the API to answer before saying the CSV did not come (a cold API takes
 *  seconds). Only the answer is timed: on a slow connection the file itself may take longer. */
const WAIT_MS = 20_000;

function useCsvDownload() {
  const [outcome, setOutcome] = useState<Outcome>('idle');
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    // a new tab or window, a modified click: the browser's own
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (outcome === 'busy') return;
    const link = e.currentTarget;
    setOutcome('busy');
    void download(link.href, link.getAttribute('download') ?? '').then(setOutcome);
  };
  return { outcome, onClick, failed: outcome === 'offline' || outcome === 'error' };
}

async function download(href: string, name: string): Promise<Outcome> {
  // A timer of its own, stopped once the API answers: AbortSignal.timeout keeps running, and would cut
  // off a file still arriving.
  const giveUp = new AbortController();
  const timer = window.setTimeout(() => giveUp.abort(), WAIT_MS);
  try {
    // the API answers this site with CORS; no cookies go to it
    const res = await fetch(href, { mode: 'cors', credentials: 'omit', signal: giveUp.signal });
    window.clearTimeout(timer);
    if (!res.ok) return 'error';
    const url = URL.createObjectURL(await res.blob());
    const save = document.createElement('a');
    save.href = url;
    save.download = name;
    document.body.append(save);
    save.click();
    save.remove();
    // the browser has taken the file by then
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    return 'idle';
  } catch {
    // No answer in time, or the file broke off on the way. "No connection" only when the browser knows
    // it has none (a slow or unreachable API on a working connection is the API's failure, not the
    // visitor's).
    return navigator.onLine === false ? 'offline' : 'error';
  }
}

/** One link per CSV language, straight to the API's attachment (it carries the audit in its header
 *  cells, so a forwarded file keeps its qualification). A failed download's note follows the group,
 *  as a line of its own in the brief's row of actions (brief.css places it). */
export function CsvButtons({ csvPath, district, days, window: w }: { csvPath: string | null | undefined; district: string; days: number; window: BriefWindow | null }) {
  const { outcome, onClick, failed } = useCsvDownload();
  return (
    <>
      <div className="br-csv" role="group" aria-labelledby="br-csv-label">
        <span className="br-csv__label" id="br-csv-label">
          Villages as CSV
        </span>
        {LANGS.map((l) => (
          <a
            key={l.code}
            className="br-button"
            href={csvHref(csvPath, district, days, l.code)}
            download={csvFileName(district, w, l.code)}
            hrefLang={l.code}
            type="text/csv"
            title={`${district}: every village of the window, in ${l.english} (CSV for Excel)`}
            onClick={onClick}
            aria-busy={outcome === 'busy' || undefined}
          >
            <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14">
              <path d="M7 1.75v7M4 6l3 3 3-3M2.25 11.75h9.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span lang={l.code}>{l.label}</span>
          </a>
        ))}
      </div>
      {failed ? (
        <p className="br-csvnote" role="alert">
          {outcome === 'offline' ? 'Couldn’t download the CSV: no connection. Try again once you are back online.' : 'Couldn’t download the CSV: the API did not send it. Try again in a moment.'}
        </p>
      ) : null}
    </>
  );
}

/** A district's CSV on the index, in the table's last column; a failed download says so under it. */
export function CsvRowLink({ href, fileName, lang, district, children }: { href: string; fileName: string; lang: CsvLang; district: string; children: ReactNode }) {
  const { outcome, onClick, failed } = useCsvDownload();
  return (
    <>
      <a className="br-rowlink" href={href} download={fileName} hrefLang={lang} type="text/csv" onClick={onClick} aria-busy={outcome === 'busy' || undefined}>
        {children}
      </a>
      {failed ? (
        <span className="br-csvnote" role="alert">
          <span className="sr-only">{`The CSV for ${district} did not download: `}</span>
          {outcome === 'offline' ? 'No connection' : 'Couldn’t download'}
        </span>
      ) : null}
    </>
  );
}
