'use client';

import { Fragment, useCallback, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';

import { Freshness } from '@/components/ui/states';
import { shortUrl } from '@/components/case/audit';
import { Listen } from '@/components/voice/Listen';
import { speakable } from '@/components/voice/voice';
import { dateIST, dateTimeIST, number } from '@/lib/format';
import { LANGS, langEnglish } from '@/lib/langs';
import { modelName } from '@/lib/models';

import { devanagari, gurmukhi } from './fonts';
import type { Bulletin, BulletinLangCode, BulletinSet } from './types';

/**
 * Today's bulletin (/bulletin, the app's "report" screen): the text the investigator wrote from the
 * live data, in English, Hindi or Punjabi, with what the number checker found beside it and every
 * source it cites. A language whose text failed the script check (verification.script_ok false) or
 * that the API could not serve keeps its tab, marked, and shows one honest line instead of the text.
 * The switch changes the URL (?lang=) so a reader can share the language they chose.
 */

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
  const english = langEnglish(code);
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

/** A range of figures takes an en dash ("67.9%-95.5%" reads "67.9%–95.5%"); an ISO date keeps its hyphens. */
const rangeDash = (text: string): string => text.replace(/\b\d{4}-\d{2}-\d{2}\b|(\d%?)-(?=\d)/g, (all, a?: string) => (a ? `${a}–` : all));

/**
 * The text's figures as the rest of the site writes them: Indian digit grouping ("106682.8 ha" reads
 * "1,06,682.8 ha") and an en dash in a range. Display only: no value is rounded and no word changes,
 * so every figure still reads exactly as the number check matched it. A year (1900 to 2100, no
 * decimals), a figure with a leading zero ("01 Nov") and an id after "case", "scar" or "id" stay as written.
 */
export function displayFigures(text: string): string {
  return rangeDash(text).replace(/(^|[^\p{L}\p{N}#.,:/_-])(\d{4,})(\.\d+)?(?![\p{N},])/gu, (all, pre: string, int: string, dec: string | undefined, offset: number, whole: string) => {
    if (int.startsWith('0')) return all;
    if (int.length === 4 && !dec && Number(int) >= 1900 && Number(int) <= 2100) return all;
    if (/(case|scar|id)\s*$/i.test(whole.slice(Math.max(0, offset - 8), offset + pre.length))) return all;
    return `${pre}${Number(int).toLocaleString('en-IN')}${dec ?? ''}`;
  });
}

/** What Listen says first when the number check did not pass, as the verdict card does on screen. */
const CHECK_FAILED: Record<BulletinLangCode, string> = {
  en: 'The number check did not pass for this bulletin.',
  hi: 'इस बुलेटिन के अंकों की जाँच पास नहीं हुई।',
  pa: 'ਇਸ ਬੁਲੇਟਿਨ ਦੇ ਅੰਕਾਂ ਦੀ ਜਾਂਚ ਪਾਸ ਨਹੀਂ ਹੋਈ।',
};

/** Body text with the run's citation markers ("[22]") turned into links to the source list. */
function withMarkers(text: string, markers: Set<string>, prefix: string): ReactNode[] {
  // "31 Oct [2]." reads as "31 Oct²." : the marker sits against the word it cites.
  return displayFigures(text).replace(/\s+(\[\d+\])/g, '$1').split(/(\[\d+\])/g).map((part, i) => {
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
  // The language on screen, read aloud in its own voice: title, summary, then each section. A
  // bulletin whose number check did not pass says so first, as the verdict card does. The figures are
  // read as written (only a range's hyphen becomes a dash): a voice can misread Indian digit grouping.
  const english = langEnglish(lang);
  const spoken = b
    ? speakable(
        [b.verification?.passed === false ? CHECK_FAILED[lang] : null, b.title, b.summary, ...b.sections.flatMap((s) => [s.heading, s.body])]
          .filter((x): x is string => Boolean(x))
          .map(rangeDash)
          .join('\n'),
        lang,
      )
    : '';
  const print = (where: 'top' | 'foot') =>
    b ? (
      <button type="button" className={`button-secondary print-${where}`} onClick={() => window.print()}>
        Print this bulletin
      </button>
    ) : null;

  return (
    <>
      <div className="title-row">
        <div className="title-row__text">
          <h1>{day ? `Daily bulletin, ${day}` : 'Daily bulletin'}</h1>
          {any ? (
            <p className="finding">
              Written by the BlindSpot investigator ({any.model ? modelName(any.model) : 'model not stated'}) from the data as of {asOf(any.data_as_of) ?? dateTimeIST(any.generated_at, { suffix: true })}, then checked
              number by number against the sources it cites.
            </p>
          ) : null}
        </div>
        <div className="title-row__actions" data-print="hide">
          {print('top')}
          {any ? <Freshness resp={any} cadenceMs={DAY_MS} verb="written" /> : null}
        </div>
      </div>

      <div className="lang-bar" data-print="hide">
        <div className="lang-tabs" role="tablist" aria-label="Language">
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
        {b ? <Listen id={`bulletin-${lang}`} text={spoken} lang={lang} what={`the ${english} bulletin`} size="regular" className="lang-listen" /> : null}
      </div>

      <div className="bulletin-grid" id="bulletin-panel" role="tabpanel" aria-labelledby={`tab-${lang}`}>
        <article className={`bulletin ${FONT[lang]}`} lang={lang}>
          {b ? (
            <>
              <p className="bulletin__title">{displayFigures(b.title)}</p>
              <p className="bulletin__summary">{withMarkers(b.summary, markers, prefix)}</p>
              {b.sections.map((sct, i) => (
                <section key={i} className="bulletin__section">
                  <h2>{displayFigures(sct.heading)}</h2>
                  <p>{withMarkers(sct.body, markers, prefix)}</p>
                </section>
              ))}
              {/* The audit of candidate unseen burns stands with the text that counts them, not at the foot of the rail. */}
              <footer className="bulletin__foot" lang="en">
                <h2>Reading the figures</h2>
                {auditNote}
              </footer>
              {/* on a phone Print comes last, after the text it prints (the one at the top is hidden there) */}
              <div className="print-foot__row" data-print="hide">
                {print('foot')}
              </div>
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
