import Link from 'next/link';

import { AGENT_STATS_DAYS, PATHS, type AgentGroupStats, type AgentStats } from '@/lib/api';
import { serverGet } from '@/lib/api-server';
import { dateIST, NBSP, number, percent, plural as count } from '@/lib/format';
import { langEnglish } from '@/lib/langs';
import { modelName } from '@/lib/models';

import { Failed, Figure, Quote } from './sections';

/**
 * "How the investigator is checked": what happens to every answer before it is final, as the API's
 * agent/runner.py and agent/verifier.py do it, and how often each check acted, counted by
 * GET /api/agent/stats from BlindSpot's own stored runs. Every count is the API's; the words only
 * say what each one counts (the API's `definitions`). A check that older runs did not record is
 * given as "n of m" with the older runs named, never folded into a share (contract notes, "Agent and
 * bulletin").
 *
 * The flow of checks is drawn whether or not the counts load, so the method never waits on them.
 */

/** A count, whole or (a median of an even number of runs) with one decimal. */
const n = (v: number) => number(v, { decimals: Number.isInteger(v) ? 0 : 1 });

/** A duration in seconds for a large figure: one decimal under a minute ("11.6"), whole above it. */
const secs = (ms: number) => (ms < 59_950 ? number(ms / 1000, { decimals: 1 }) : number(Math.round(ms / 1000)));
/** "11.6 s" under a minute, "2 min 1 s" above it; each number kept on one line with its unit. */
function took(ms: number): string {
  if (ms < 59_950) return `${secs(ms)}${NBSP}s`;
  const s = Math.round(ms / 1000);
  return s % 60 ? `${Math.floor(s / 60)}${NBSP}min ${s % 60}${NBSP}s` : `${s / 60}${NBSP}min`;
}

/** "it", "both", "all 3" or "2 of the 3": how many of `whole` drafts went on to pass. */
function ofThose(part: number, whole: number): string {
  if (part === whole) return whole === 1 ? 'it' : whole === 2 ? 'both' : `all ${n(whole)}`;
  return part === 0 ? 'none' : `${n(part)} of the ${n(whole)}`;
}

/** "Gemini 3.5 Flash-Lite wrote 20 answers and Gemini 3.6 Flash 1." from the models of the final texts. */
function writers(models: AgentGroupStats['models'], noun: [string, string]): string | null {
  const m = models.filter((x) => x.runs > 0);
  if (!m.length) return null;
  const [first, ...rest] = m;
  if (!rest.length) return `${modelName(first.model)} wrote ${first.runs === 1 ? `the one ${noun[0]}` : `all ${count(first.runs, noun[0], noun[1])}`}.`;
  const lead = `${modelName(first.model)} wrote ${count(first.runs, noun[0], noun[1])}`;
  const tail = rest.map((x) => `${modelName(x.model)} ${n(x.runs)}`);
  return `${lead}${tail.length > 1 ? `, ${tail.slice(0, -1).join(', ')}` : ''} and ${tail[tail.length - 1]}.`;
}

/** " 10 earlier answers predate the check." when some runs were stored before it existed. */
const earlier = (unknown: number, noun: [string, string]) => (unknown > 0 ? ` ${count(unknown, `earlier ${noun[0]} predates`, `earlier ${noun[1]} predate`)} the check.` : '');

/** The API's basis, for people: without the table's name, or the note on which source it lists. */
const basisWords = (t: string) =>
  t
    .replace(/\s*\(the \w+ table\)/g, '')
    .replace(/\s*The source listed is[^;.]*[;.]/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim();

// ---------------------------------------------------------------------------------------------
// the checks, in the order the runner applies them

type Step = { title: string; body: string; count: string | null };
const ANSWER: [string, string] = ['answer', 'answers'];

function steps(q: AgentGroupStats | null): Step[] {
  const fig = q?.figures;
  const hon = q?.honesty;
  const cor = q?.corrected_figures;
  const tools = q?.tool_calls.median;
  const by = q ? writers(q.models, ANSWER) : null;
  return [
    {
      title: 'Gemini, from the tools',
      body: "The model gathers its facts by calling BlindSpot's tools, which read this site's own API. Each tool's output carries numbered sources for the answer to cite.",
      count: by || tools != null ? [by, tools != null ? `Runs made a median of ${count(tools, 'tool call', 'tool calls')}.` : null].filter(Boolean).join(' ') : null,
    },
    {
      title: 'Every figure looked up',
      body: 'Code takes every number, time and date out of the draft and looks for each, within rounding, in what the tools returned, the question or the clock. A cited figure must come from the tool its marker points to.',
      count: fig && fig.checked > 0 ? `${n(fig.matched)} of the ${n(fig.checked)} figures in the final answers were found (${percent(fig.matched_share, 1)}).${earlier(fig.unknown, ANSWER)}` : null,
    },
    {
      title: 'Four honesty rules',
      body: "Candidate unseen burns are named as candidates, with the audit's precision and its 95% interval. No raw risk scores. The ranking's AUC only over all pass pairs. FIRMS detections are never called crop fires.",
      count:
        hon && hon.of > 0
          ? `${hon.first_draft_issues === 0 ? `None of ${n(hon.of)} first drafts broke one.` : `${n(hon.first_draft_issues)} of ${n(hon.of)} first drafts broke one; in the end ${ofThose(hon.then_passed, hon.first_draft_issues)} kept every rule.`}${earlier(hon.unknown, ANSWER)}`
          : null,
    },
    {
      title: 'One rewrite',
      body: 'A draft with a figure found nowhere, or a broken rule, goes back to the model once, with the list. The rewrite is kept unless it has more problems than the draft.',
      count:
        cor && cor.of > 0
          ? `${cor.runs === 0 ? 'No answer has been corrected for a figure found nowhere.' : `${count(cor.runs, 'answer was', 'answers were')} corrected for a figure found nowhere; ${ofThose(cor.then_passed, cor.runs)} then passed.`}${earlier(cor.unknown, ANSWER)}`
          : null,
    },
    {
      title: 'Then fixed code',
      body: 'A rule still broken after the rewrite is not left to the model: code rewrites those sentences from the tool outputs, in fixed words.',
      count: hon && hon.of > 0 ? (hon.fallback_sentences > 0 ? `${count(hon.fallback_sentences, 'sentence', 'sentences')} rewritten this way, in ${count(hon.fallback_runs, 'answer', 'answers')}.` : 'No sentence has needed it yet.') : null,
    },
  ];
}

/** From one step to the next: drawn pointing right, turned to point down where the steps stack. */
function Arrow() {
  return (
    <svg className="m-flow__arrow" viewBox="0 0 28 12" aria-hidden="true" focusable="false">
      <path d="M0 6H21" fill="none" stroke="var(--line)" strokeWidth="1.5" />
      <path d="M20 1.5L27 6L20 10.5Z" fill="var(--line)" />
    </svg>
  );
}

function Flow({ q, days }: { q: AgentGroupStats | null; days: number }) {
  const list = steps(q);
  return (
    <figure className="m-flow" aria-labelledby="m-flow-h">
      <p className="m-chart__title" id="m-flow-h">
        What happens to an answer before it is final
      </p>
      <ol className="m-flow__list">
        {list.map((s, i) => (
          <li key={s.title} className="m-flow__step">
            {i > 0 ? <Arrow /> : null}
            <p className="m-flow__title">{s.title}</p>
            <p className="m-flow__body">{s.body}</p>
            {s.count ? <p className="m-flow__count">{s.count}</p> : null}
          </li>
        ))}
        <li className="m-flow__step m-flow__step--end">
          <Arrow />
          <p className="m-flow__title">The checked answer stays</p>
          <p className="m-flow__body">The draft streams on screen as it is written. What stays is the checked answer, with a line under it: every figure found, or the ones that were not.</p>
        </li>
      </ol>
      {q && list.some((s) => s.count) ? <figcaption className="m-caption">{`The last line of each step counts what it did to the investigator's ${count(q.answered, 'answer', 'answers')} of the last ${n(days)} days.`}</figcaption> : null}
    </figure>
  );
}

// ---------------------------------------------------------------------------------------------
// the bulletin's record, and how to read both

const BULLETIN: [string, string] = ['bulletin', 'bulletins'];

function Bulletin({ b }: { b: AgentGroupStats }) {
  const c = b.corrected_figures;
  const facts: [string, string][] = [['Bulletins written', `${n(b.answered)} of ${count(b.runs, 'run', 'runs')}${b.failed ? `; ${n(b.failed)} failed` : ''}`]];
  if (b.figures.checked > 0) facts.push(["Figures found in the tools' outputs", `${n(b.figures.matched)} of ${n(b.figures.checked)} (${percent(b.figures.matched_share, 1)})`]);
  if (b.all_matched.of > 0) facts.push(['Passed the number check', `${n(b.all_matched.runs)} of ${n(b.all_matched.of)}`]);
  if (c.of > 0) {
    const after = c.runs === 0 ? '' : c.then_passed === 0 ? (c.runs === 1 ? '; it did not then pass' : '; none then passed') : `; ${ofThose(c.then_passed, c.runs)} then passed`;
    facts.push(['First draft held a figure found nowhere', `${n(c.runs)} of ${n(c.of)}${after}`]);
  }
  if (b.honesty.of > 0) facts.push(['First draft broke an honesty rule', `${n(b.honesty.first_draft_issues)} of ${n(b.honesty.of)}${b.honesty.unknown > 0 ? `; ${count(b.honesty.unknown, 'earlier bulletin predates', 'earlier bulletins predate')} the check` : ''}`]);
  if (b.duration_ms.p50 != null) facts.push(['Median time to write and check', `${took(b.duration_ms.p50)}${b.duration_ms.p90 != null ? `; 9 in 10 within ${took(b.duration_ms.p90)}` : ''}`]);
  if (b.tool_calls.median != null) facts.push(['Median tool calls', n(b.tool_calls.median)]);
  const by = writers(b.models, BULLETIN);
  if (by) facts.push(['Final text', by.replace(/\.$/, '')]);
  return (
    <div className="m-record">
      <h3 className="m-subhead">The daily bulletin, checked the same way</h3>
      <p className="m-body">Each bulletin is written in English from the same tools, then translated, and each translation is checked against its English. One difference: a sentence that holds a figure found nowhere is removed, not rewritten.</p>
      <dl className="m-facts">
        {facts.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd className="nums">{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Caveats({ basis, q, b }: { basis: string | null; q: AgentGroupStats; b: AgentGroupStats | null }) {
  const first = [q.first_run_at, b?.first_run_at].filter((t): t is string => !!t).sort()[0];
  const points = q.answered > 0 ? Math.round(100 / q.answered) : null;
  const langs = q.languages.filter((l) => l.runs > 0);
  const lines: string[] = [];
  if (points != null && q.answered < 100) {
    lines.push(`These are small numbers. The record starts on ${dateIST(first)}, with ${count(q.answered, 'answer', 'answers')}${b ? ` and ${count(b.answered, 'bulletin', 'bulletins')}` : ''}, so one answer moves a share by about ${count(points, 'point', 'points')}.`);
  }
  if (langs.length === 1 && langs[0].lang === 'en') {
    lines.push(`${langs[0].runs === 1 ? 'The one question was' : `All ${n(langs[0].runs)} questions were`} asked in English, so the record says nothing yet about answers in Hindi or Punjabi.`);
  } else if (langs.length) {
    lines.push(`Questions by language: ${langs.map((l) => `${langEnglish(l.lang)} ${n(l.runs)}`).join(', ')}.`);
  }
  lines.push("And a figure found in the tools' outputs repeats BlindSpot's data faithfully; it is not thereby right. How right the data is, is the rest of this page.");
  return (
    <div>
      {basis ? <Quote label="How these are counted">{basisWords(basis)}</Quote> : null}
      <p className="m-body">{lines.join(' ')}</p>
      <p className="m-body">
        <Link className="m-link m-link--tap" href="/live">
          Ask the investigator on the live map
        </Link>
        , and read the check under its answer.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------

export async function Investigator() {
  const r = await serverGet<AgentStats>(PATHS.agentStats, { days: AGENT_STATS_DAYS });
  const d = r.ok ? r.data : null;
  const q = d?.questions && d.questions.runs > 0 ? d.questions : null;
  const b = d?.bulletins && d.bulletins.runs > 0 ? d.bulletins : null;
  const days = d?.days ?? AGENT_STATS_DAYS;
  // "the first on 30 Oct" only when the record is younger than the window
  const young = !!(q?.first_run_at && d && Date.parse(q.first_run_at) - Date.parse(d.window.from_utc) > 86_400_000);
  const asked = q?.kinds.ask;
  const featured = q?.kinds.featured;
  const wait = q?.duration_ms.p50 ?? null;
  return (
    <>
      {!r.ok ? <Failed what="the investigator's record" r={r} /> : null}
      {d && !q ? <p className="m-failed">{`No question was put to the investigator in the last ${n(days)} days, so there is nothing to count yet.`}</p> : null}
      {q ? (
        <div className="m-figures">
          <Figure value={n(q.runs)}>
            {`${q.runs === 1 ? 'question was' : 'questions were'} put to the investigator in the last ${n(days)} days${young ? `, the first on ${dateIST(q.first_run_at)}` : ''}`}
            {asked != null && featured ? `: ${n(asked)} on this site and ${n(featured)} by the daily job` : ''}
            {`. ${q.failed === 0 ? (q.runs === 1 ? 'It was answered.' : `All ${n(q.runs)} were answered.`) : `${count(q.answered, 'was answered', 'were answered')}; ${count(q.failed, 'run', 'runs')} ended without an answer.`}`}
          </Figure>
          {q.all_matched.of > 0 ? (
            <Figure value={n(q.all_matched.runs)} unit={` of ${n(q.all_matched.of)}`}>
              {'answers passed the number check: every figure found in what the tools returned, and every citation a real source.'}
              {q.all_matched.without_figures > 0 ? ` ${q.all_matched.without_figures === 1 ? 'One of them stated' : `${n(q.all_matched.without_figures)} of them stated`} no figure at all.` : ''}
              {earlier(q.all_matched.unknown, ANSWER)}
            </Figure>
          ) : null}
          {wait != null ? (
            <Figure value={secs(wait)} unit=" s">
              {`was the median wait from question to checked answer${q.duration_ms.p90 != null ? `; 9 in 10 took ${took(q.duration_ms.p90)} or less` : ''}.`}
            </Figure>
          ) : null}
        </div>
      ) : null}
      <Flow q={q} days={days} />
      {q && d ? (
        <div className="m-cols m-cols--table">
          {b ? <Bulletin b={b} /> : <p className="m-body">{`No daily bulletin was written in the last ${n(days)} days.`}</p>}
          <Caveats basis={d.basis} q={q} b={b} />
        </div>
      ) : null}
    </>
  );
}
