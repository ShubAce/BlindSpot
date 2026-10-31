'use client';

import type { ReactNode } from 'react';

import { precisionText, useLiveHeadline, useSourcesLive } from '@/components/live/data';
import { dateTimeIST, number } from '@/lib/format';

import type { Cite, CiteSource, Investigation, NamedScar, Verification } from './model';
import { evidenceOf, mentionsUnseen } from './model';

/**
 * The answer as the investigator wrote it (a verdict, its evidence as bullets, one caveat), with
 * each citation marker linked to its source below, then what the number check found, then the
 * fields it named and its sources. While it streams, the draft shows with its markers unlinked.
 */

const MARKER = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

function Inline({ text: raw, cites, idp }: { text: string; cites: Map<string, Cite> | null; idp: string }) {
  const text = raw.replace(/\*\*/g, '');
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
          return c ? (
            <a key={num} className="iv-cite" href={`#${idp}-src-${num}`} title={c.source.label} aria-label={`Source ${num}: ${c.source.label}`}>
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

export function AnswerText({ text, citations, idp }: { text: string; citations: Cite[] | null; idp: string }) {
  const cites = citations ? new Map(citations.map((c) => [String(c.marker), c])) : null;
  return (
    <>
      {blocks(text).map((b, i) =>
        b.kind === 'list' ? (
          <ul key={i} className="iv-list">
            {b.items.map((item, j) => (
              <li key={j}>
                <Inline text={item} cites={cites} idp={idp} />
              </li>
            ))}
          </ul>
        ) : (
          <p key={i} className={b.kind === 'lead' ? 'iv-lead' : b.kind === 'caveat' ? 'iv-caveat' : 'iv-para'}>
            <Inline text={b.text} cites={cites} idp={idp} />
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

function Sources({ citations, idp }: { citations: Cite[]; idp: string }) {
  const live = useSourcesLive();
  const docs = new Map<string, string>();
  for (const s of (live.data?.sources ?? []) as unknown as { key: string; docs_url?: string | null }[]) if (s.docs_url) docs.set(s.key, s.docs_url);
  const sorted = [...citations].sort((a, b) => Number(a.marker) - Number(b.marker));
  return (
    <section className="iv-sources" aria-label="Sources of this answer">
      <h4 className="iv-subhead">Sources</h4>
      <ul>
        {sorted.map((c) => {
          const href = sourceHref(c.source, docs);
          return (
            <li key={String(c.marker)} id={`${idp}-src-${c.marker}`} tabIndex={-1}>
              <span className="iv-srcMarker">{c.marker}</span>
              <span className="iv-srcBody">
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer" className="iv-srcLink">
                    {c.source.label}
                  </a>
                ) : (
                  <span>{c.source.label}</span>
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

export function AnswerPanel({ inv, idp, onOpenCase }: { inv: Investigation; idp: string; onOpenCase: (id: number) => void }) {
  const text = inv.answer?.text ?? inv.draft;
  const streaming = inv.status === 'running' && !inv.answer;
  const ev = evidenceOf(inv.tools);
  const has = Boolean(text) || inv.verification || inv.answer;
  return (
    <section className="iv-answer" aria-labelledby={`${idp}-answer`} data-empty={has ? undefined : ''}>
      <h4 className="iv-subhead" id={`${idp}-answer`}>
        Answer
        {streaming && text ? <span className="iv-headNote"> writing</span> : null}
        {inv.answer && inv.verification == null && inv.status === 'running' ? <span className="iv-headNote"> checking the figures</span> : null}
      </h4>
      <div aria-live="polite" aria-busy={inv.status === 'running'} className="iv-answerText">
        {text ? <AnswerText text={text} citations={inv.answer?.citations ?? null} idp={idp} /> : null}
        {inv.verification ? <Verdict v={inv.verification} /> : null}
      </div>
      {inv.answer && mentionsUnseen(inv) ? <Precision /> : null}
      {inv.status !== 'running' ? <Fields scars={ev.scars} onOpen={onOpenCase} /> : null}
      {inv.answer?.citations.length ? <Sources citations={inv.answer.citations} idp={idp} /> : null}
    </section>
  );
}

