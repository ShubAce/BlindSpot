'use client';

import { useMemo, type ReactNode } from 'react';

import { precisionText, useLiveHeadline, useSourcesLive } from '@/components/live/data';
import { Listen } from '@/components/voice/Listen';
import { scriptLang, speakable } from '@/components/voice/voice';
import { dateTimeIST, number } from '@/lib/format';

import type { Cite, CiteSource, Investigation, NamedScar, Verification } from './model';
import { evidenceOf, mentionsUnseen } from './model';

/**
 * The answer as the investigator wrote it (a verdict, its evidence as bullets, one caveat), with
 * each citation marker linked to its source below, then what the number check found, then the
 * fields it named and its sources. While it streams, the draft shows with its markers unlinked.
 * A finished answer can be listened to (the device's voice for its language).
 */

const MARKER = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

type SourceNames = { name: (src: CiteSource) => string; docs: Map<string, string> };

/** A label that is only a source key ("s2_cog", "stac_e84"), as some stored citations carry. */
const keyLike = (label: string, key: string) => label === key || /^[a-z0-9]+(?:_[a-z0-9]+)+$/.test(label);

function hostOf(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return null;
  }
}

/**
 * Each source by a name a reader knows: the citation's own label, unless it is just the source's
 * key; then the name GET /api/sources gives that key (the request the sources panel already makes);
 * then the address it was read from. Never the key itself.
 */
function useSourceNames(): SourceNames {
  const live = useSourcesLive();
  return useMemo(() => {
    const labels = new Map<string, string>();
    const docs = new Map<string, string>();
    for (const s of live.data?.sources ?? []) {
      const label = s.label?.trim();
      if (label && !keyLike(label, s.key)) labels.set(s.key, label);
      if (s.docs_url) docs.set(s.key, s.docs_url);
    }
    const name = (src: CiteSource): string => {
      const own = src.label?.trim();
      if (own && !keyLike(own, src.key)) return own;
      return labels.get(src.key) ?? hostOf(src.url) ?? 'A source BlindSpot reads';
    };
    return { name, docs };
  }, [live.data]);
}

function Inline({ text: raw, cites, idp, names }: { text: string; cites: Map<string, Cite> | null; idp: string; names: SourceNames }) {
  // "detection [2]." reads as "detection²." : the marker sits against the word it cites, as in the bulletin
  const text = raw.replace(/\*\*/g, '').replace(/\s+(\[\d+(?:\s*,\s*\d+)*\])/g, '$1');
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(MARKER)) {
    const at = m.index ?? 0;
    out.push(text.slice(last, at));
    const nums = m[1].split(',').map((x) => x.trim());
    out.push(
      <span key={at} className="iv-cites">
        {nums.map((num) => {
          const c = cites?.get(num);
          const label = c ? names.name(c.source) : '';
          return c ? (
            <a key={num} className="iv-cite" href={`#${idp}-src-${num}`} title={label} aria-label={`Source ${num}: ${label}`}>
              {num}
            </a>
          ) : (
            <span key={num} className="iv-cite" data-pending="">
              {num}
            </span>
          );
        })}
      </span>,
    );
    last = at + m[0].length;
  }
  out.push(text.slice(last));
  return <>{out}</>;
}

type Block = { kind: 'lead' | 'p' | 'caveat'; text: string } | { kind: 'list'; items: string[] };

function blocks(text: string): Block[] {
  const out: Block[] = [];
  for (const raw of text.split(/\n+/)) {
    const line = raw.trim();
    if (!line) continue;
    const bullet = /^[-•*]\s+(.*)$/.exec(line);
    if (bullet) {
      const prev = out[out.length - 1];
      if (prev?.kind === 'list') prev.items.push(bullet[1]);
      else out.push({ kind: 'list', items: [bullet[1]] });
    } else if (/^caveat\s*:/i.test(line)) {
      out.push({ kind: 'caveat', text: line });
    } else {
      out.push({ kind: out.length ? 'p' : 'lead', text: line });
    }
  }
  return out;
}

export function AnswerText({ text, citations, idp, names }: { text: string; citations: Cite[] | null; idp: string; names: SourceNames }) {
  const cites = citations ? new Map(citations.map((c) => [String(c.marker), c])) : null;
  return (
    <>
      {blocks(text).map((b, i) =>
        b.kind === 'list' ? (
          <ul key={i} className="iv-list">
            {b.items.map((item, j) => (
              <li key={j}>
                <Inline text={item} cites={cites} idp={idp} names={names} />
              </li>
            ))}
          </ul>
        ) : (
          <p key={i} className={b.kind === 'lead' ? 'iv-lead' : b.kind === 'caveat' ? 'iv-caveat' : 'iv-para'}>
            <Inline text={b.text} cites={cites} idp={idp} names={names} />
          </p>
        ),
      )}
    </>
  );
}

/** "All 10 figures in this answer match the data." or which ones did not. */
export function verificationWords(v: Verification): { tone: 'positive' | 'warning'; text: string } {
  const fig = (k: number) => (k === 1 ? 'figure' : 'figures');
  if (v.checked === 0) return { tone: 'positive', text: 'This answer states no figures to check.' };
  if (v.passed && v.verified === v.checked) {
    return { tone: 'positive', text: v.checked === 1 ? 'The one figure in this answer matches the data.' : `All ${number(v.checked)} figures in this answer match the data.` };
  }
  const which = v.unverified.length ? ` Not found in the data: ${v.unverified.join('; ')}.` : '';
  return { tone: 'warning', text: `${number(v.verified)} of ${number(v.checked)} ${fig(v.checked)} in this answer match the data.${which}` };
}

function Verdict({ v }: { v: Verification }) {
  const w = verificationWords(v);
  const first = v.first_pass_unverified ?? [];
  const rules = v.first_pass_honesty ?? [];
  const bad = v.bad_markers ?? [];
  return (
    <div className="iv-check" data-tone={w.tone}>
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" className="iv-checkMark">
        {w.tone === 'positive' ? (
          <path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        ) : (
          <path d="M8 3.5v5.5M8 11.6v.4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        )}
      </svg>
      <div>
        <p className="iv-checkText">{w.text}</p>
        {v.revised && first.length ? (
          <details className="iv-more">
            <summary>
              The first draft had {first.length === 1 ? 'a figure' : `${number(first.length)} figures`} the check did not find; the investigator rewrote it before answering.
            </summary>
            <ul>
              {first.map((f, i) => (
                <li key={i}>{f}</li>
              ))}
            </ul>
          </details>
        ) : null}
        {rules.length ? (
          <details className="iv-more">
            <summary>
              The first draft broke {rules.length === 1 ? 'an honesty rule' : `${number(rules.length)} honesty rules`}; it was rewritten before you saw it.
            </summary>
            <ul>
              {rules.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </details>
        ) : null}
        {bad.length ? <p className="iv-checkSub">Citation {bad.length === 1 ? 'marker' : 'markers'} with no source: {bad.map((b) => `[${b}]`).join(', ')}.</p> : null}
      </div>
    </div>
  );
}

/** A source link that works: masked URLs (keys shown as ***) would fail, so they point at the source's docs page. */
function sourceHref(src: CiteSource, docs: Map<string, string>): string | null {
  if (src.url && !src.url.includes('***')) return src.url;
  return docs.get(src.key) ?? null;
}

function Sources({ citations, idp, names }: { citations: Cite[]; idp: string; names: SourceNames }) {
  const sorted = [...citations].sort((a, b) => Number(a.marker) - Number(b.marker));
  return (
    <section className="iv-sources" aria-label="Sources of this answer">
      <h4 className="iv-subhead">Sources</h4>
      <ul>
        {sorted.map((c) => {
          const href = sourceHref(c.source, names.docs);
          const label = names.name(c.source);
          return (
            <li key={String(c.marker)} id={`${idp}-src-${c.marker}`} tabIndex={-1}>
              <span className="iv-srcMarker">{c.marker}</span>
              <span className="iv-srcBody">
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer" className="iv-srcLink">
                    {label}
                  </a>
                ) : (
                  <span>{label}</span>
                )}
                {c.source.fetched_at ? <span className="iv-srcTime"> fetched {dateTimeIST(c.source.fetched_at, { suffix: true })}</span> : null}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function scarWords(s: NamedScar): string {
  return s.unseen === true ? 'candidate unseen burn' : s.unseen === false ? 'a fire alert explains it' : 'not yet checked against fire alerts';
}

function Fields({ scars, onOpen }: { scars: NamedScar[]; onOpen: (id: number) => void }) {
  if (!scars.length) return null;
  // largest first: the fields that matter most to the answer
  const shown = [...scars].sort((a, b) => (b.areaHa ?? 0) - (a.areaHa ?? 0)).slice(0, 6);
  return (
    <section className="iv-fields" aria-label="Fields this answer names">
      <h4 className="iv-subhead">Fields it looked at, outlined on the map</h4>
      <ul>
        {shown.map((s) => (
          <li key={s.id}>
            <button type="button" className="iv-field" onClick={() => onOpen(s.id)}>
              <span className="iv-fieldName">{s.village ? [`Near ${s.village}`, s.district].filter(Boolean).join(', ') : s.district ?? `Scar ${s.id}`}</span>
              <span className="iv-fieldMeta">
                {`Scar ${s.id} · `}
                {s.areaHa != null ? `${number(s.areaHa, { decimals: 1 })} ha · ` : ''}
                {scarWords(s)}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {scars.length > shown.length ? <p className="iv-note">and {number(scars.length - shown.length)} more on the map</p> : null}
    </section>
  );
}

/** The precision of the candidate unseen burns, read from the headline, never typed in. `inline`
 *  for a line inside a button (an example's preview), where a <p> may not go. */
export function Precision({ inline = false }: { inline?: boolean }) {
  const head = useLiveHeadline();
  const text = precisionText(head.data?.unseen_burns);
  if (!text) return null;
  if (inline) return <span className="iv-exMeta">Candidate unseen burns: {text}.</span>;
  return <p className="iv-note">Candidate unseen burns are Sentinel-2 burn scars with no fire alert: {text}.</p>;
}

/** The answer for the ear, in its own language; an English one ends with what the number check found. */
function spoken(inv: Investigation): { text: string; lang: ReturnType<typeof scriptLang> } | null {
  if (inv.status !== 'done' || !inv.answer?.text) return null;
  const lang = scriptLang(inv.answer.text);
  const check = lang === 'en' && inv.verification ? ` ${verificationWords(inv.verification).text}` : '';
  return { text: `${speakable(inv.answer.text, lang)}${check}`, lang };
}

/**
 * A draft the number check never saw: the run stopped, or the connection dropped before the checked
 * answer arrived. It is kept (the reader saw it stream) but folded away and named for what it is,
 * so it never stands in for an answer.
 */
function UncheckedDraft({ inv, idp, names }: { inv: Investigation; idp: string; names: SourceNames }) {
  return (
    <>
      <p className="iv-note iv-draftNote">
        {inv.recovering
          ? 'The draft that streamed before the connection dropped has not been checked yet; the checked answer replaces it once it loads.'
          : inv.status === 'stopped'
            ? 'Stopped before the number check, so the draft below is not an answer: none of its figures was checked against the data.'
            : 'The run ended before the number check reached this page, so the draft below is not an answer: none of its figures was checked against the data.'}
      </p>
      <details className="iv-draft">
        <summary>Show the unchecked draft</summary>
        <div className="iv-draftText">
          <AnswerText text={inv.draft} citations={null} idp={`${idp}-draft`} names={names} />
        </div>
      </details>
    </>
  );
}

export function AnswerPanel({ inv, idp, onOpenCase }: { inv: Investigation; idp: string; onOpenCase: (id: number) => void }) {
  const names = useSourceNames();
  // a draft is shown as it streams; once the run has ended (or its stream broke) without the checked
  // answer, it is only ever shown folded, as an unchecked draft
  const unchecked = !inv.answer && Boolean(inv.draft) && (inv.status !== 'running' || inv.recovering);
  const text = inv.answer?.text ?? (unchecked ? '' : inv.draft);
  const streaming = inv.status === 'running' && !inv.answer && !inv.recovering;
  const ev = useMemo(() => evidenceOf(inv.tools), [inv.tools]);
  const has = Boolean(text) || unchecked || inv.verification || inv.answer;
  // once per state of the run, not on every tick of the console's clock
  const voice = useMemo(() => spoken(inv), [inv]);
  return (
    <section className="iv-answer" aria-labelledby={`${idp}-answer`} data-empty={has ? undefined : ''}>
      <div className="iv-answerHead">
        <h4 className="iv-subhead" id={`${idp}-answer`}>
          Answer
          {streaming && text ? <span className="iv-headNote"> writing</span> : null}
          {inv.answer && inv.verification == null && inv.status === 'running' ? <span className="iv-headNote"> checking the figures</span> : null}
          {unchecked ? <span className="iv-headNote">{inv.recovering ? ' loading the checked answer' : ' none checked'}</span> : null}
        </h4>
        {voice ? <Listen id={`answer-${inv.key}`} text={voice.text} lang={voice.lang} what="this answer" className="iv-listen" /> : null}
      </div>
      <div aria-live="polite" aria-busy={inv.status === 'running'} className="iv-answerText">
        {text ? <AnswerText text={text} citations={inv.answer?.citations ?? null} idp={idp} names={names} /> : null}
        {unchecked ? <UncheckedDraft inv={inv} idp={idp} names={names} /> : null}
        {inv.verification ? <Verdict v={inv.verification} /> : null}
      </div>
      {inv.answer && mentionsUnseen(inv) ? <Precision /> : null}
      {inv.status !== 'running' ? <Fields scars={ev.scars} onOpen={onOpenCase} /> : null}
      {inv.answer?.citations.length ? <Sources citations={inv.answer.citations} idp={idp} names={names} /> : null}
    </section>
  );
}

