'use client';

import { Suspense, useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'next/navigation';

import { ErrorNote, EmptyNote, Skeleton } from '@/components/ui/states';
import { dateTimeIST, duration, number, relative, timeIST } from '@/lib/format';
import { useNow } from '@/lib/live';

import { AnswerPanel } from './Answer';
import { budgetOf, useAgentStatus, useExamples, type Budget, type ExampleRun } from './data';
import { modelName, normaliseQuestion } from './describe';
import type { Investigation } from './model';
import { useFollow } from './follow';
import { Steps } from './Steps';
import { useInvestigator, type HistoryItem } from './use-investigator';
import './investigator.css';

/**
 * The investigator console in mission control: ask a question, watch Gemini pick BlindSpot's tools
 * (each call in plain words and as code), see the map follow the evidence, and read an answer whose
 * every figure is cited and checked against the data. Verified example runs play from storage, so
 * they cost no Gemini call; a typed question runs live. `/live?ask=<question>` starts one on load.
 *
 * Wide screens: a column of its own beside the ranked list, with its own scroll and a collapse
 * control. Below 1100px it is a block under the panels.
 */

const COLLAPSE_KEY = 'blindspot:investigator:collapsed';

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSE_KEY) === '1';
  } catch {
    return false;
  }
}
function writeCollapsed(v: boolean) {
  try {
    window.localStorage.setItem(COLLAPSE_KEY, v ? '1' : '0');
  } catch {
    // storage blocked: the choice lasts for this page
  }
}

const isNarrow = () => typeof window !== 'undefined' && window.matchMedia?.('(max-width: 1100px)').matches === true;

export function Investigator() {
  const idp = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const iv = useInvestigator();
  const examples = useExamples();
  const status = useAgentStatus();
  const now = useNow(1_000);
  const budget = budgetOf(status.data, now);
  const [draft, setDraft] = useState('');
  const [collapsed, setCollapsed] = useState(false);
  const [linked, setLinked] = useState<string | null>(null);
  const root = useRef<HTMLElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const questionRef = useRef<HTMLHeadingElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // the stored choice is only readable in the browser
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (readCollapsed()) setCollapsed(true);
  }, []);

  const setCollapsedAndStore = (v: boolean) => {
    setCollapsed(v);
    writeCollapsed(v);
  };

  const exampleFor = useCallback(
    (q: string): ExampleRun | undefined => {
      const key = normaliseQuestion(q);
      return examples.data?.examples.find((e) => normaliseQuestion(e.question) === key);
    },
    [examples.data],
  );

  /** Start a question: a verified run of the same question plays from storage; anything else runs live. */
  const start = useCallback(
    (q: string, opts: { live?: boolean; focus?: boolean } = {}) => {
      const question = q.replace(/\s+/g, ' ').trim();
      if (!question) return;
      setCollapsed(false);
      const ex = opts.live ? undefined : exampleFor(question);
      if (ex) void iv.play(ex.id, ex.question);
      else void iv.ask(question);
      if (opts.focus) requestAnimationFrame(() => questionRef.current?.focus({ preventScroll: true }));
    },
    [exampleFor, iv],
  );

  // the deep link: wait (briefly) for the examples, so a verified question plays instead of costing a live call
  const [waitedOut, setWaitedOut] = useState(false);
  useEffect(() => {
    if (!linked) return;
    const t = setTimeout(() => setWaitedOut(true), 3_000);
    return () => clearTimeout(t);
  }, [linked]);
  useEffect(() => {
    if (!linked) return;
    if (!examples.data && !examples.error && !waitedOut) return;
    const q = linked;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLinked(null);
    start(q);
    if (isNarrow()) {
      const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      requestAnimationFrame(() => root.current?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' }));
    }
  }, [linked, examples.data, examples.error, waitedOut, start]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = draft.trim();
    if (!q) {
      inputRef.current?.focus();
      return;
    }
    start(q);
    setDraft('');
  };

  const back = () => {
    iv.close();
    requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }));
  };

  useFollow(body, iv.inv, `${idp}-answer`);

  if (collapsed) {
    return (
      <section className="iv" data-collapsed="" aria-label="Investigator">
        <Suspense fallback={null}>
          <AskFromUrl onAsk={setLinked} />
        </Suspense>
        <button type="button" className="iv-open" onClick={() => setCollapsedAndStore(false)} aria-expanded="false" aria-controls={`${idp}-console`}>
          <svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18">
            <path d="M11 4.5L6.5 9l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="iv-openText">Investigator</span>
        </button>
      </section>
    );
  }

  const inv = iv.inv;
  const running = inv?.status === 'running';

  return (
    <section ref={root} className="iv lv-card" id={`${idp}-console`} aria-labelledby={`${idp}-title`}>
      <Suspense fallback={null}>
        <AskFromUrl onAsk={setLinked} />
      </Suspense>
      <header className="iv-head">
        <h2 className="lv-cardTitle" id={`${idp}-title`}>
          Investigator
        </h2>
        <BudgetPill budget={budget} loading={!status.data && !status.error} />
        <button type="button" className="iv-collapse lv-iconButton" onClick={() => setCollapsedAndStore(true)} aria-expanded="true" aria-label="Hide the investigator">
          <svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18">
            <path d="M7 4.5L11.5 9 7 13.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </header>

      <div className="iv-body" ref={body}>
        {inv ? (
          <Transcript
            inv={inv}
            idp={idp}
            budget={budget}
            cacheMinutes={status.data?.answer_cache_minutes ?? null}
            now={now}
            questionRef={questionRef}
            onBack={back}
            onStop={iv.stop}
            onSkip={iv.skip}
            onLive={() => start(inv.question, { live: true })}
            onReplay={() => inv.runId && void iv.play(inv.runId, inv.question)}
            onReapply={iv.reapply}
            onOpenCase={iv.openCase}
          />
        ) : (
          <Start
            idp={idp}
            examples={examples}
            history={iv.history}
            onExample={(ex) => {
              setCollapsed(false);
              void iv.play(ex.id, ex.question);
              requestAnimationFrame(() => questionRef.current?.focus({ preventScroll: true }));
            }}
            onHistory={(h) => {
              if (h.runId) void iv.play(h.runId, h.question, { paced: false });
              else start(h.question, { live: true, focus: true });
              requestAnimationFrame(() => questionRef.current?.focus({ preventScroll: true }));
            }}
          />
        )}
      </div>

      <form className="iv-composer" onSubmit={onSubmit} role="search" aria-label="Ask the investigator">
        <label htmlFor={`${idp}-q`} className="sr-only">
          Your question
        </label>
        <div className="iv-askRow">
          <input
            ref={inputRef}
            id={`${idp}-q`}
            className="iv-input"
            type="text"
            name="question"
            autoComplete="off"
            enterKeyHint="send"
            maxLength={500}
            placeholder="Ask about fires, burn scars, smoke or air…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-describedby={`${idp}-budget`}
          />
          {running && !draft.trim() ? (
            <button type="button" className="iv-button" data-variant="stop" onClick={iv.stop}>
              Stop
            </button>
          ) : (
            <button type="submit" className="iv-button">
              Ask
            </button>
          )}
        </div>
        <BudgetLine id={`${idp}-budget`} budget={budget} error={!!status.error && !status.data} />
      </form>
    </section>
  );
}

/** Reads `?ask=` once, hands it over and takes it off the address, so a reload does not ask again. */
function AskFromUrl({ onAsk }: { onAsk: (q: string) => void }) {
  const params = useSearchParams();
  const q = params.get('ask');
  const seen = useRef<string | null>(null);
  useEffect(() => {
    if (!q || seen.current === q) return;
    seen.current = q;
    onAsk(q);
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('ask');
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    } catch {
      // leave the address as it is
    }
  }, [q, onAsk]);
  return null;
}

// ---------------------------------------------------------------------------------------------
// before a question: the verified questions and this session's

function Start({
  idp,
  examples,
  history,
  onExample,
  onHistory,
}: {
  idp: string;
  examples: ReturnType<typeof useExamples>;
  history: HistoryItem[];
  onExample: (ex: ExampleRun) => void;
  onHistory: (h: HistoryItem) => void;
}) {
  const list = examples.data?.examples ?? [];
  return (
    <>
      <p className="iv-intro">
        Gemini answers from BlindSpot&rsquo;s live data only. You see each tool it calls, the map follows what it finds, and every figure in the answer is cited and checked against the data
        before you read it.
      </p>
      <section aria-labelledby={`${idp}-ex`} className="iv-group">
        <h3 className="iv-subhead" id={`${idp}-ex`}>
          Verified questions
        </h3>
        <p className="iv-subNote">Each plays a stored, checked run: no new Gemini call.</p>
        {examples.error && !examples.data ? (
          <ErrorNote what="the verified questions" error={examples.error} onRetry={() => void examples.mutate()} style={{ fontSize: 13 }} />
        ) : !examples.data ? (
          <ul className="iv-examples" aria-hidden="true">
            {[0, 1, 2].map((i) => (
              <li key={i} className="iv-exSkeleton">
                <Skeleton width="92%" height={14} />
                <Skeleton width="70%" height={14} />
                <Skeleton width="55%" height={11} />
              </li>
            ))}
          </ul>
        ) : list.length === 0 ? (
          <EmptyNote style={{ fontSize: 13 }}>No verified runs are stored yet. Ask a question below; it runs live.</EmptyNote>
        ) : (
          <ul className="iv-examples">
            {list.map((ex) => (
              <li key={ex.id}>
                <button type="button" className="iv-ex" onClick={() => onExample(ex)}>
                  <span className="iv-exQ">{ex.question}</span>
                  {ex.answer ? <span className="iv-exAnswer">{firstSentence(ex.answer)}</span> : null}
                  <span className="iv-exMeta">
                    {[
                      ex.verification ? checkedWords(ex.verification.verified, ex.verification.checked) : null,
                      ex.tools_used?.length ? `${number(ex.tools_used.filter((t) => t !== 'show_on_map').length)} tools` : null,
                      `answered ${dateTimeIST(ex.started_at, { suffix: true })}`,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {history.length ? (
        <section aria-labelledby={`${idp}-hist`} className="iv-group">
          <h3 className="iv-subhead" id={`${idp}-hist`}>
            Asked in this session
          </h3>
          <ul className="iv-examples" data-size="small">
            {history.map((h) => (
              <li key={h.key}>
                <button type="button" className="iv-ex" onClick={() => onHistory(h)}>
                  <span className="iv-exQ">{h.question}</span>
                  <span className="iv-exMeta">
                    {[
                      h.mode === 'live' ? 'live run' : h.mode === 'cached' ? 'replayed by the server' : 'stored run',
                      h.status === 'stopped' ? 'stopped' : h.status === 'error' ? 'did not finish' : h.verified ? checkedWords(h.verified.verified, h.verified.checked) : null,
                      timeIST(h.at, { suffix: true }),
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}

/** The verdict line of a stored answer, without its citation markers (they are linked in the run itself). */
const firstSentence = (text: string) => text.split(/\n/)[0].replace(/\s*\[\d+(?:\s*,\s*\d+)*\]/g, '').replace(/\*\*/g, '').trim();

const checkedWords = (verified: number, checked: number) =>
  checked === 0 ? 'no figures to check' : verified === checked ? `${number(checked)} of ${number(checked)} figures checked` : `${number(verified)} of ${number(checked)} figures matched`;

// ---------------------------------------------------------------------------------------------
// one question

function ModeLine({ inv, cacheMinutes, now }: { inv: Investigation; cacheMinutes: number | null; now: Date | null }) {
  const running = inv.status === 'running';
  if (inv.mode === 'stored') {
    return (
      <p className="iv-mode" data-mode="stored">
        Replay of a verified run{inv.narratedAt ? ` from ${dateTimeIST(inv.narratedAt, { suffix: true })}` : ''}
        {inv.durationMs != null ? `; it took ${duration(inv.durationMs)} live` : ''}. No new Gemini call.
      </p>
    );
  }
  if (inv.mode === 'cached') {
    return (
      <p className="iv-mode" data-mode="cached">
        Replayed by the server: the same question was answered{inv.narratedAt && now ? ` ${relative(inv.narratedAt, now)}, at ${timeIST(inv.narratedAt, { suffix: true })}` : ' a few minutes ago'}
        {cacheMinutes != null ? `, and answers are reused for ${number(cacheMinutes)} min` : ''}. No new Gemini call.
      </p>
    );
  }
  const elapsed = running ? (now ? Math.max(0, now.getTime() - inv.startedAt) : null) : inv.durationMs ?? (inv.endedAt ? inv.endedAt - inv.startedAt : null);
  return (
    <p className="iv-mode" data-mode="live" data-running={running ? '' : undefined}>
      <span className="iv-liveDot" aria-hidden="true" />
      Live run
      {inv.model ? ` · ${modelName(inv.model)}` : ''}
      {elapsed != null ? <span className="iv-nums"> · {running ? duration(elapsed) : `answered in ${duration(elapsed)}`}</span> : null}
    </p>
  );
}

function Transcript({
  inv,
  idp,
  budget,
  cacheMinutes,
  now,
  questionRef,
  onBack,
  onStop,
  onSkip,
  onLive,
  onReplay,
  onReapply,
  onOpenCase,
}: {
  inv: Investigation;
  idp: string;
  budget: Budget;
  cacheMinutes: number | null;
  now: Date | null;
  questionRef: React.RefObject<HTMLHeadingElement | null>;
  onBack: () => void;
  onStop: () => void;
  onSkip: () => void;
  onLive: () => void;
  onReplay: () => void;
  onReapply: (action: string, payload: Record<string, unknown>) => boolean;
  onOpenCase: (id: number) => void;
}) {
  const running = inv.status === 'running';
  const evidenceOnly = budget.state === 'spent' || budget.state === 'off';
  return (
    <article className="iv-run" aria-labelledby={`${idp}-question`}>
      <button type="button" className="iv-back lv-linkish" onClick={onBack}>
        <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14">
          <path d="M8.5 3.5L5 7l3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        All questions
      </button>
      <h3 className="iv-question" id={`${idp}-question`} ref={questionRef} tabIndex={-1}>
        {inv.question || <Skeleton width="80%" height={18} />}
      </h3>
      <ModeLine inv={inv} cacheMinutes={cacheMinutes} now={now} />
      <div className="iv-actions">
        {running && inv.mode === 'stored' ? (
          <button type="button" className="lv-ctrl" onClick={onSkip}>
            Skip to the answer
          </button>
        ) : null}
        {running && inv.mode !== 'stored' ? (
          <button type="button" className="lv-ctrl" onClick={onStop}>
            Stop
          </button>
        ) : null}
        {!running && inv.mode !== 'live' && inv.question ? (
          <button type="button" className="lv-ctrl" onClick={onLive} title={evidenceOnly ? 'Today\'s Gemini budget is spent: a live question gets the evidence without a written answer.' : undefined}>
            Ask it again live
          </button>
        ) : null}
        {!running && inv.mode === 'stored' && inv.runId && inv.status === 'done' ? (
          <button type="button" className="lv-ctrl" onClick={onReplay}>
            Play again
          </button>
        ) : null}
      </div>

      <Steps inv={inv} onReapply={onReapply} />

      {inv.status === 'error' && inv.error ? (
        <div className="iv-error" role="alert">
          <p>{inv.error.message}</p>
          {inv.error.retryable ? (
            <button type="button" className="lv-ctrl" onClick={inv.mode === 'stored' && inv.runId && !inv.steps.length ? onReplay : onLive}>
              Try again
            </button>
          ) : null}
        </div>
      ) : null}
      {inv.status === 'stopped' ? (
        <p className="iv-note" role="status">
          Stopped. {inv.mode === 'live' ? 'The run carries on on the server and is stored; ask the same question within a few minutes to see its answer without a new call.' : ''}
        </p>
      ) : null}

      <AnswerPanel inv={inv} idp={idp} onOpenCase={onOpenCase} />
    </article>
  );
}

// ---------------------------------------------------------------------------------------------
// the Gemini budget (GET /api/agent/status)

function BudgetPill({ budget, loading }: { budget: Budget; loading: boolean }) {
  if (loading) return <Skeleton width={84} height={20} style={{ marginLeft: 'auto' }} />;
  if (budget.state === 'spent') return <span className="lv-pill iv-pill" data-tone="warning">Live budget spent</span>;
  if (budget.state === 'cooling') return <span className="lv-pill iv-pill" data-tone="warning">Gemini cooling down</span>;
  if (budget.state === 'off') return <span className="lv-pill iv-pill" data-tone="warning">Live answers off</span>;
  if (budget.state === 'open') return <span className="lv-pill iv-pill">Live answers on</span>;
  return <span className="iv-pill" />;
}

function BudgetLine({ id, budget, error }: { id: string; budget: Budget; error: boolean }) {
  let text: string;
  let tone: 'warning' | undefined;
  if (error) text = 'The investigator\'s budget could not be read; a question may still run live.';
  else if (budget.state === 'unknown') text = 'Reading the investigator\'s budget.';
  else if (budget.state === 'off') {
    text = 'No Gemini key on this server: a question gets the live evidence without a written answer. The verified questions play in full.';
    tone = 'warning';
  } else if (budget.state === 'spent') {
    text = `Today's Gemini budget is spent${budget.resetsAt ? ` until ${timeIST(budget.resetsAt, { suffix: true })}` : ''}: a new question gets the live evidence without a written answer. The verified questions play in full.`;
    tone = 'warning';
  } else if (budget.state === 'cooling') {
    text = `Gemini asked us to wait until ${timeIST(budget.until, { suffix: true })}; a question now gets the live evidence without a written answer. The verified questions play in full.`;
    tone = 'warning';
  } else {
    text = `A typed question runs live on Gemini's free tier${budget.resetsAt ? `; the daily budget resets at ${timeIST(budget.resetsAt, { suffix: true })}` : ''}.`;
  }
  return (
    <p className="iv-budget" id={id} data-tone={tone}>
      {text}
    </p>
  );
}
