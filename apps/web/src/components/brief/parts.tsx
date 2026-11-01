import Link from 'next/link';
import type { ReactNode } from 'react';

import { uniqueSources } from '@/components/case/audit';
import { DailyFreshness } from '@/components/case/DailyFreshness';
import { ProvItem } from '@/components/case/parts';
import type { Envelope } from '@/lib/api';
import { dateTimeIST, MISSING, NBSP, number } from '@/lib/format';

import { DAY_CHOICES, windowRange } from './read';
import type { BriefRiskRun, BriefWindow } from './types';

/**
 * The pieces both brief screens share. All Server Components: the window switch and the CSV language
 * are plain links (?days=, ?lang=), so they work before any JavaScript and in a printed or forwarded
 * copy of the address.
 */

/** 3, 7, 14 or 30 days of passes. The current window carries aria-current and the active fill. */
function DaysSwitch({ days, hrefFor }: { days: number; hrefFor: (d: number) => string }) {
  return (
    <nav className="br-days" aria-label="Window of Sentinel-2 passes" data-print="hide">
      {DAY_CHOICES.map((d) => (
        <Link key={d} href={hrefFor(d)} aria-current={d === days ? 'page' : undefined} className="br-days__opt">
          {d}
          <span className="br-days__unit">{`${NBSP}days`}</span>
        </Link>
      ))}
    </nav>
  );
}

/** The title row's right-hand column: the window of passes, its switch (`hrefFor` is each window's
 *  address on this screen) and how fresh the answer is. */
export function BriefScope({ data, days, hrefFor }: { data: Envelope & { window: BriefWindow | null }; days: number; hrefFor: (d: number) => string }) {
  const w = data.window;
  return (
    <div className="br-scope">
      <p className="br-scope__window nums">{w ? `Sentinel-2 passes ${windowRange(w)}` : 'No Sentinel-2 pass mapped yet'}</p>
      <DaysSwitch days={days} hrefFor={hrefFor} />
      <span className="br-scope__fresh" data-print="hide">
        {/* only what the freshness reads: the whole answer would ship to the browser a second time */}
        {/* stale, in the warning colour, only once the daily Sentinel-2 run is late (26 h, the API's rule) */}
        <DailyFreshness resp={{ generated_at: data.generated_at, sources: data.sources }} verb="updated" note="passes are processed daily" />
      </span>
    </div>
  );
}

/** " Try a longer window: 14 days, 30 days." after an empty list's sentence, or nothing when the
 *  window is already the longest offered. */
export function LongerWindows({ days, hrefFor }: { days: number; hrefFor: (d: number) => string }) {
  const longer = DAY_CHOICES.filter((d) => d > days);
  if (!longer.length) return null;
  return (
    <>
      {' Try a longer window: '}
      {longer.map((d, i) => (
        <span key={d}>
          {i ? ', ' : ''}
          <Link href={hrefFor(d)}>{`${d} days`}</Link>
        </span>
      ))}
      .
    </>
  );
}

/** A row's share of the list's largest area, as a bar (the table's figures carry the values). */
export function BarCell({ value, max }: { value: number | null | undefined; max: number }) {
  const share = max > 0 ? Math.max(0, Math.min(1, (value || 0) / max)) : 0;
  return (
    <td className="br-c-bar" aria-hidden="true">
      <span className="br-bar" style={{ width: `${(share * 100).toFixed(2)}%` }} />
    </td>
  );
}

/** The API's notes on reading the brief, in full and numbered (so a reader can point at "note 5"),
 *  on the index as on every district: the index is the page most often printed and forwarded. */
export function NotesCard({ notes }: { notes: string[] | null | undefined }) {
  if (!notes?.length) return null;
  return (
    <section className="card br-notes" aria-labelledby="br-notes-title">
      <h2 id="br-notes-title" className="card__title br-notes__title">
        How to read this brief
      </h2>
      <ol className="br-notes__list">
        {notes.map((n, i) => (
          <li key={i}>{n}</li>
        ))}
      </ol>
    </section>
  );
}

/**
 * A row's grey figures as one line, which brief.css draws in place of their cells at 900px and
 * below ("Assandh · 10 fields · 10.1 ha · best rank 17,454"). Each figure stays whole and the line
 * breaks only between figures, with no dot left at either end of a line; as separate cells, a figure
 * that wrapped took its dot to the start of the next line. Screen readers skip it and read the cells,
 * which stay in the page.
 */
export function RowLine({ parts }: { parts: (string | null | undefined)[] }) {
  return (
    <span className="br-line" aria-hidden="true">
      <span className="br-line__in">
        {parts
          .filter((p): p is string => !!p)
          .map((p, i) => (
            <span key={i}>{p}</span>
          ))}
      </span>
    </span>
  );
}

/** The screen when its one read failed: the title stays, one sentence says why, and Retry reloads the
 *  same address (a plain link, so it works before JavaScript). No figure is drawn without the API.
 *  The page still arrives with status 200 (it streams), so its failure note carries data-load-failed:
 *  the offline worker (public/sw.js) never lets a page marked so replace a good copy it saved. */
export function BriefError({ title, what, message, retryHref, eyebrow }: { title: string; what: string; message: string; retryHref: string; eyebrow?: ReactNode }) {
  return (
    <div className="title-row">
      <div className="title-row__text">
        {eyebrow}
        <h1>{title}</h1>
        <p className="finding" role="status" data-load-failed="">
          {`Couldn't load ${what}. ${message}`}
        </p>
        <p className="finding-audit">The brief shows no figure until the API answers; it never fills a gap with an estimate.</p>
        <p className="not-found__links">
          <a className="br-button br-button--primary" href={retryHref}>
            Retry
          </a>
          <Link className="br-textlink" href="/live">
            Open the live map
          </Link>
        </p>
      </div>
    </div>
  );
}

/** When the numbers were made and from what: the brief's own time, its window's newest pass, the
 *  risk run behind the ranks, and every upstream input with its fetch time. */
export function SourcesFoot({ data, window: w, risk }: { data: Envelope; window: BriefWindow | null; risk: BriefRiskRun | null }) {
  const sources = uniqueSources(data.sources);
  return (
    <section className="card br-foot" aria-labelledby="br-sources-title">
      <header className="card__head">
        <h2 id="br-sources-title" className="card__title">
          Sources and freshness
        </h2>
        <div className="card__aside">{`${sources.length} inputs`}</div>
      </header>
      <dl className="br-fresh nums">
        <div className="br-fresh__item">
          <dt>Brief built</dt>
          <dd>{dateTimeIST(data.generated_at, { year: true, suffix: true })}</dd>
        </div>
        <div className="br-fresh__item">
          <dt>Newest Sentinel-2 pass</dt>
          <dd>{w?.newest_pass ? dateTimeIST(w.newest_pass, { year: true, suffix: true }) : MISSING}</dd>
        </div>
        <div className="br-fresh__item">
          <dt>Burn-risk run</dt>
          <dd>
            {risk?.as_of
              ? `${dateTimeIST(risk.as_of, { year: true, suffix: true })}${risk.model ? `, ${risk.model}` : ''}${risk.fields_ranked != null ? `, ${number(risk.fields_ranked)} fields ranked` : ''}`
              : 'No run yet'}
          </dd>
        </div>
      </dl>
      {sources.length ? (
        <ol className="prov prov--cols br-prov">
          {sources.map((s, i) => (
            <ProvItem key={`${s.key}-${i}`} source={s} label={s.label || s.key} />
          ))}
        </ol>
      ) : (
        <p className="card__note">The API listed no sources with this answer.</p>
      )}
    </section>
  );
}
