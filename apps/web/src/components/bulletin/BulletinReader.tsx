'use client';

import { Fragment, useCallback, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';

import { Freshness } from '@/components/ui/states';
import { shortUrl } from '@/components/case/audit';
import { dateIST, dateTimeIST, number } from '@/lib/format';

import { devanagari, gurmukhi } from './fonts';
import type { Bulletin, BulletinLangCode, BulletinSet } from './types';

/**
 * Today's bulletin (/bulletin, the app's "report" screen): the text the investigator wrote from the
 * live data, in English, Hindi or Punjabi, with what the number checker found beside it and every
 * source it cites. A language whose text failed the script check (verification.script_ok false) or
 * that the API could not serve keeps its tab, marked, and shows one honest line instead of the text.
 * The switch changes the URL (?lang=) so a reader can share the language they chose.
 */

const LANGS: { code: BulletinLangCode; label: string; english: string }[] = [
  { code: 'en', label: 'English', english: 'English' },
  { code: 'hi', label: 'हिन्दी', english: 'Hindi' },
  { code: 'pa', label: 'ਪੰਜਾਬੀ', english: 'Punjabi' },
];

/** Work Sans stays first (Latin letters, digits, markers); the Noto face fills in the Indian script. */
const FONT: Record<BulletinLangCode, string> = { en: '', hi: `${devanagari.variable} script-deva`, pa: `${gurmukhi.variable} script-guru` };
const DAY_MS = 86_400_000;

/** "2026-10-31 04:58 IST" (the API's own IST stamp) as "31 Oct, 04:58 IST"; anything else unchanged. */
function asOf(s: string | null | undefined): string | null {
  const m = s?.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) IST$/);
  return m ? dateTimeIST(`${m[1]}T${m[2]}:00+05:30`, { suffix: true }) : (s ?? null);
}

/** A source URL that opens only with an access key: the API masked the key (***), or the upstream
 *  wants one in a header (OpenAQ). */
const keyedUrl = (url: string): boolean => url.includes('***') || /^https:\/\/api\.openaq\.org\//.test(url);

/** A language that cannot be shown says why: held back by the script check, absent today, or not loaded. */
type Usable = { ok: true; data: Bulletin } | { ok: false; reason: string; flag: 'held back' | 'none today' | 'not loaded' };

function usable(set: BulletinSet, code: BulletinLangCode): Usable {
  const r = set[code];
  const english = LANGS.find((l) => l.code === code)?.english ?? code;
  if (!r.ok)
    return r.error.status === 404
      ? { ok: false, flag: 'none today', reason: `There is no ${english} bulletin today.` }
      : { ok: false, flag: 'not loaded', reason: `The ${english} bulletin could not be loaded. ${r.error.message}` };
  if (r.data.lang !== code) return { ok: false, flag: 'none today', reason: `The API has no ${english} bulletin today.` };
  // The API's own verdict comes first: `available: false` with its one-sentence reason.
  if (r.data.available === false) return { ok: false, flag: 'held back', reason: r.data.reason || `The ${english} bulletin is held back today: it did not pass the script check.` };
  if (r.data.verification && r.data.verification.script_ok === false) {
    const words = r.data.verification.foreign_script_words;
    return {
      ok: false,
      flag: 'held back',
      reason: `The ${english} bulletin is held back today: the checker found letters from another script in it${words.length ? ` (${words.join(', ')})` : ''}.`,
    };
  }
  return { ok: true, data: r.data };
}

/** Body text with the run's citation markers ("[22]") turned into links to the source list. */
function withMarkers(text: string, markers: Set<string>, prefix: string): ReactNode[] {
  // "31 Oct [2]." reads as "31 Oct²." : the marker sits against the word it cites.
  return text.replace(/\s+(\[\d+\])/g, '$1').split(/(\[\d+\])/g).map((part, i) => {
    const m = part.match(/^\[(\d+)\]$/);
    if (!m) return <Fragment key={i}>{part}</Fragment>;
    if (!markers.has(m[1])) return <Fragment key={i}>{part}</Fragment>;
    return (
      <sup key={i} className="cite">
        <a href={`#${prefix}-${m[1]}`} aria-label={`source ${m[1]}`}>
          {m[1]}
        </a>
      </sup>
    );
  });
}

function Verification({ v, lang }: { v: Bulletin['verification']; lang: BulletinLangCode }) {
  if (!v) return <p className="card__note">The API sent no verification for this bulletin.</p>;
  const removed = v.removed_sentences ?? [];
  return (
    <>
      <p className={`verdict ${v.passed ? 'is-pass' : 'is-fail'}`}>
        <span className="verdict__mark" aria-hidden="true" />
        {v.passed
          ? `${number(v.verified)} of ${number(v.checked)} checked numbers matched their source${v.uncited.length ? `; ${number(v.uncited.length)} had no citation` : ''}`
          : 'The number check did not pass'}
      </p>
      <dl className="figs">
        <div className="figs__row">
          <dt>Numbers checked</dt>
          <dd className="nums">{number(v.checked)}</dd>
        </div>
        <div className="figs__row">
          <dt>Matched to their source</dt>
          <dd className="nums">{number(v.verified)}</dd>
        </div>
        <div className="figs__row">
          <dt>Not matched</dt>
          <dd className="nums">
            {number(v.unverified.length)}
            {v.unverified.length ? <span className="figs__list">{v.unverified.join(', ')}</span> : null}
          </dd>
        </div>
        <div className="figs__row">
          <dt>Numbers without a citation</dt>
          <dd className="nums">
            {number(v.uncited.length)}
            {v.uncited.length ? <span className="figs__list">{`the figure ${v.uncited.map((u) => `“${u}”`).join(', ')}`}</span> : null}
          </dd>
        </div>
        <div className="figs__row">
          <dt>Sentences removed</dt>
          <dd className="nums">{number(removed.length)}</dd>
        </div>
        <div className="figs__row">
          <dt>Script check</dt>
          <dd>{v.script_ok ? 'Passed' : 'Failed'}</dd>
        </div>
      </dl>
      {removed.length ? (
        <details className="removed">
          <summary>Show the removed sentences</summary>
          <ul lang={lang} className={FONT[lang]}>
            {removed.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
          <p className="card__note">The checker could not match a number in each of these to the data, so it took them out.</p>
        </details>
      ) : null}
    </>
  );
}

export function BulletinReader({ set, initial, auditNote }: { set: BulletinSet; initial: BulletinLangCode; auditNote: ReactNode }) {
  const states = Object.fromEntries(LANGS.map((l) => [l.code, usable(set, l.code)])) as Record<BulletinLangCode, Usable>;
  const [lang, setLang] = useState<BulletinLangCode>(initial);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const choose = useCallback((code: BulletinLangCode) => {
    setLang(code);
    try {
      const url = new URL(window.location.href);
      if (code === 'en') url.searchParams.delete('lang');
      else url.searchParams.set('lang', code);
      window.history.replaceState(window.history.state, '', url);
    } catch {
      /* the URL is a convenience; the switch works without it */
    }
  }, []);
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const n = LANGS.length;
    const j = e.key === 'ArrowRight' ? (i + 1) % n : e.key === 'ArrowLeft' ? (i - 1 + n) % n : e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : -1;
    if (j < 0) return;
    e.preventDefault();
    choose(LANGS[j].code);
    tabs.current[j]?.focus();
  };

  // The frame (date, model, freshness) comes from the language on screen; each language has its own
  // run and model. Only when that one is not readable does it come from the first that is.
  const anyOk = LANGS.map((l) => states[l.code]).find((s): s is { ok: true; data: Bulletin } => s.ok);
  const cur = states[lang];
  const b = cur.ok ? cur.data : null;
  const any = b ?? anyOk?.data ?? LANGS.map((l) => set[l.code]).find((r) => r.ok)?.data ?? null;
  const markers = new Set((b?.citations ?? []).map((c) => String(c.marker)));
  const prefix = `src-${lang}`;
  const day = any?.date ? dateIST(`${any.date}T12:00:00+05:30`, { weekday: true, year: true }) : null;

  return (
    <>
      <div className="title-row">
        <div className="title-row__text">
          <h1>{day ? `Daily bulletin, ${day}` : 'Daily bulletin'}</h1>
          {any ? (
            <p className="finding">
              Written by the BlindSpot investigator ({any.model ?? 'model not stated'}) from the data as of {asOf(any.data_as_of) ?? dateTimeIST(any.generated_at, { suffix: true })}, then checked
              number by number against the sources it cites.
            </p>
          ) : null}
        </div>
        <div className="title-row__actions" data-print="hide">
          {b ? (
            <button type="button" className="button-secondary" onClick={() => window.print()}>
              Print this bulletin
            </button>
          ) : null}
          {any ? <Freshness resp={any} cadenceMs={DAY_MS} verb="written" /> : null}
        </div>
      </div>

      <div className="lang-tabs" role="tablist" aria-label="Language" data-print="hide">
        {LANGS.map((l, i) => {
          const s = states[l.code];
          return (
            <button
              key={l.code}
              ref={(el) => {
                tabs.current[i] = el;
              }}
              type="button"
              role="tab"
              id={`tab-${l.code}`}
              aria-selected={lang === l.code}
              aria-controls="bulletin-panel"
              tabIndex={lang === l.code ? 0 : -1}
              lang={l.code}
              className={`lang-tab ${FONT[l.code]}${lang === l.code ? ' selected-line' : ''}${s.ok ? '' : ' is-held'}`}
              onClick={() => choose(l.code)}
              onKeyDown={(e) => onKey(e, i)}
            >
              {l.label}
              {s.ok ? null : <span className="lang-tab__flag">{s.flag}</span>}
            </button>
          );
        })}
      </div>

      <div className="bulletin-grid" id="bulletin-panel" role="tabpanel" aria-labelledby={`tab-${lang}`}>
        <article className={`bulletin ${FONT[lang]}`} lang={lang}>
          {b ? (
            <>
              <p className="bulletin__title">{b.title}</p>
              <p className="bulletin__summary">{withMarkers(b.summary, markers, prefix)}</p>
              {b.sections.map((sct, i) => (
                <section key={i} className="bulletin__section">
                  <h2>{sct.heading}</h2>
                  <p>{withMarkers(sct.body, markers, prefix)}</p>
                </section>
              ))}
              {/* The audit of candidate unseen burns stands with the text that counts them, not at the foot of the rail. */}
              <footer className="bulletin__foot" lang="en">
                <h2>Reading the figures</h2>
                {auditNote}
              </footer>
            </>
          ) : (
            <p className="bulletin__held" lang="en" role="status">
              {cur.ok ? null : cur.reason}
              {anyOk ? ' The other languages are above.' : ' '}
              {!anyOk && !cur.ok && cur.flag === 'not loaded' ? (
                <a className="text-button" href="">
                  Retry
                </a>
              ) : null}
            </p>
          )}
        </article>

        <aside className="bulletin-rail" aria-label="Checks and sources">
          <section className="card" aria-labelledby="check-title">
            <header className="card__head">
              <h2 id="check-title" className="card__title">
                Checked against the data
              </h2>
            </header>
            {b ? <Verification v={b.verification} lang={lang} /> : <p className="card__note">Nothing to check while this language is not shown.</p>}
          </section>

          <section className="card" aria-labelledby="sources-title">
            <header className="card__head">
              <h2 id="sources-title" className="card__title">
                Sources cited
              </h2>
              {b ? <span className="card__aside">{number(b.citations.length)}</span> : null}
            </header>
            {b?.citations.length ? (
              <ol className="prov">
                {[...b.citations]
                  .sort((x, y) => Number(x.marker) - Number(y.marker))
                  .map((c) => (
                    <li key={String(c.marker)} id={`${prefix}-${c.marker}`} className="prov__item">
                      <span className="prov__label">
                        <span className="cite-num nums">{c.marker}</span> {c.source.label}
                      </span>
                      <span className="prov__meta nums">{c.source.fetched_at ? `fetched ${dateTimeIST(c.source.fetched_at, { suffix: true })}` : 'fetch time not recorded'}</span>
                      {c.source.url ? (
                        keyedUrl(c.source.url) ? (
                          // the API masked its key (***) or the upstream wants one: shown, not linked, so it never opens an error page
                          <span className="prov__url" title={`${c.source.url} (needs an access key, so not a link)`}>
                            {shortUrl(c.source.url)}
                          </span>
                        ) : (
                          <a className="prov__url" href={c.source.url} target="_blank" rel="noreferrer noopener" title={c.source.url}>
                            {shortUrl(c.source.url)}
                          </a>
                        )
                      ) : null}
                    </li>
                  ))}
              </ol>
            ) : (
              <p className="card__note">{b ? 'This bulletin cites no sources.' : 'No sources while this language is not shown.'}</p>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}
