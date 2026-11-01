'use client';

import Link from 'next/link';
import { Suspense, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'next/navigation';

import { prefersReducedMotion } from '@/components/map/clock';
import { isOffline, useConnectivity } from '@/components/pwa/connectivity';
import { useDictation, type Dictation } from '@/components/voice/voice';
import { ErrorNote, EmptyNote, Skeleton } from '@/components/ui/states';
import { dateTimeIST, duration, number, relative, timeIST } from '@/lib/format';
import { LANGS, langEnglish } from '@/lib/langs';
import { useNow } from '@/lib/live';
import { modelName } from '@/lib/models';
import { FOLLOW_UP_TOO_OLD } from '@/lib/sse';
import { plainText } from '@/lib/text';

import { AnswerPanel, Precision } from './Answer';
import { budgetOf, curateExamples, guardrailWords, useAgentStats, useAgentStatus, useExamples, type Budget, type ExampleRun } from './data';
import { normaliseQuestion } from './describe';
import { canFollowUp, followUpOf, windowEnd, type FollowUp, type Investigation, type RunError } from './model';
import { useFollow } from './follow';
import { Steps } from './Steps';
import { useInvestigator, type HistoryItem } from './use-investigator';
import './investigator.css';

/**
 * The investigator console in mission control: ask a question (typed, or spoken where the browser
 * can listen), watch Gemini pick BlindSpot's tools (each call in plain words and as code), see the
 * map follow the evidence, and read (or listen to) an answer whose every figure is cited and checked
 * against the data. An answered run can be followed up while the API keeps it open: the new question
 * carries the earlier question and answer as context, and its figures are fetched and checked again.
 * Verified example runs play from storage, so they cost no Gemini call; a typed question runs live.
 *
 * Links: `/live?ask=<question>` starts one on load (a verified question plays instead of running);
 * `/live?draft=<question>` only puts the words in the box, focused, and never starts anything.
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
const sizedByCss = () => typeof CSS !== 'undefined' && CSS.supports?.('field-sizing', 'content') === true;
/** A question as it is sent and matched: one line, no stray spaces. */
const tidyQuestion = (q: string) => q.replace(/\s+/g, ' ').trim();

const MAX_QUESTION = 500;

export function Investigator() {
  const idp = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const iv = useInvestigator();
  const examples = useExamples();
  const status = useAgentStatus();
  const now = useNow(1_000);
  // Offline (Wi-Fi with no internet counts) a new question cannot run; a verified run this browser saved still plays.
  const offline = isOffline(useConnectivity());
  const budget = budgetOf(status.data, now);
  const [draft, setDraft] = useState('');
  const [collapsed, setCollapsed] = useState(false);
  const [linked, setLinked] = useState<string | null>(null);
  /** The answered run the box is writing a follow-up to (null: a question on its own). */
  const [followTo, setFollowTo] = useState<FollowUp | null>(null);
  /** One line under the box: why a question was not sent. */
  const [note, setNote] = useState<string | null>(null);
  /** Bumped to focus the box once it is on screen (after a ?draft= link, or "Ask a follow-up"). */
  const [focusBox, setFocusBox] = useState(0);
  const urlSeen = useRef<string | null>(null);
  const root = useRef<HTMLElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const questionRef = useRef<HTMLHeadingElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const dict = useDictation(setDraft);

  // The box grows with its words up to four lines, so a long or spoken question can be read before
  // it is sent. CSS does it where field-sizing exists; elsewhere it is measured here.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el || sizedByCss()) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight + 2, 112)}px`;
  }, [draft, collapsed]);

  useEffect(() => {
    // the stored choice is only readable in the browser
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (readCollapsed()) setCollapsed(true);
  }, []);

  useEffect(() => {
    if (!focusBox) return;
    const el = inputRef.current;
    if (!el) return;
    el.focus({ preventScroll: true });
    el.setSelectionRange(el.value.length, el.value.length);
    // below 1100px the console is in the page flow: bring the box to the middle of the screen
    if (isNarrow()) el.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
  }, [focusBox]);

  const setCollapsedAndStore = (v: boolean) => {
    // a hidden console takes no more words: the microphone ends with it
    if (v && dict.listening) dict.abort();
    setCollapsed(v);
    writeCollapsed(v);
  };

  // the verified runs by their normalised question, built once per list (the first of a question wins)
  const byQuestion = useMemo(() => {
    const m = new Map<string, ExampleRun>();
    for (const e of examples.data?.examples ?? []) {
      const key = normaliseQuestion(e.question);
      if (key && !m.has(key)) m.set(key, e);
    }
    return m;
  }, [examples.data]);
  const exampleFor = useCallback((q: string): ExampleRun | undefined => byQuestion.get(normaliseQuestion(q)), [byQuestion]);

  /** A question the server refused, or could not take, goes back into the box: the words are never lost. */
  const handBack = useCallback(
    (question: string, followUp: FollowUp | null) => (err: RunError) => {
      setDraft((d) => (d.trim() ? d : question));
      // A failure worth retrying (the earlier answer could not be read just now, a 429, a server
      // error, no connection) keeps the box a follow-up, so the next Ask sends the thread again
      // instead of a context-free question. Only a thread the server rejected (400, 422) is dropped.
      if (followUp && err.retryable) setFollowTo(followUp);
    },
    [],
  );

  /** Start a question: a verified run of the same question plays from storage; anything else runs live. */
  const start = useCallback(
    (q: string, opts: { live?: boolean; focus?: boolean } = {}) => {
      const question = tidyQuestion(q);
      if (!question) return;
      setCollapsed(false);
      setFollowTo(null);
      const ex = opts.live ? undefined : exampleFor(question);
      if (ex) void iv.play(ex.id, ex.question);
      else void iv.ask(question, { onNotAsked: handBack(question, null) });
      if (opts.focus) requestAnimationFrame(() => questionRef.current?.focus({ preventScroll: true }));
    },
    [exampleFor, handBack, iv],
  );

  /** Ask a follow-up of an answered run: always live (a follow-up is never a stored example). */
  const askFollowUp = useCallback(
    (q: string, fu: FollowUp) => {
      const question = tidyQuestion(q);
      if (!question) return;
      setCollapsed(false);
      setFollowTo(null);
      void iv.ask(question, { followUp: fu, onNotAsked: handBack(question, fu) });
    },
    [handBack, iv],
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
      requestAnimationFrame(() => root.current?.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' }));
    }
  }, [linked, examples.data, examples.error, waitedOut, start]);

  /** `?draft=`: the words go in the box, focused; nothing is sent (the free Gemini quota is small). */
  const prefill = useCallback((q: string) => {
    const text = tidyQuestion(q).slice(0, MAX_QUESTION);
    if (!text) return;
    setCollapsed(false);
    setFollowTo(null);
    setNote(null);
    setDraft(text);
    setFocusBox((n) => n + 1);
  }, []);

  // Offline, a typed question can still play when it is a verified one this browser saved.
  const replayable = !followTo && Boolean(draft.trim()) && Boolean(exampleFor(draft));
  const askBlocked = offline && !replayable;
  // the earlier run's window closed while the follow-up was being written: the box says so at once
  const followEnd = windowEnd(followTo?.until);
  const followClosed = followEnd != null && now != null && followEnd <= now.getTime();

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = tidyQuestion(draft);
    if (!q) {
      inputRef.current?.focus();
      return;
    }
    if (askBlocked) return;
    // no more words land in the box once it is sent
    if (dict.listening) dict.abort();
    setNote(null);
    if (followTo) {
      // checked against the clock now, not the label's last tick
      if (followEnd != null && followEnd <= Date.now()) {
        // the window closed while it was being written: say so, and keep every word
        setFollowTo(null);
        setNote(FOLLOW_UP_TOO_OLD);
        return;
      }
      askFollowUp(q, followTo);
    } else {
      start(q);
    }
    setDraft('');
  };

  const back = () => {
    iv.close();
    setFollowTo(null);
    setNote(null);
    requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }));
  };

  const followUp = (inv: Investigation) => {
    const fu = followUpOf(inv);
    if (!fu) return;
    setFollowTo(fu);
    setNote(null);
    setFocusBox((n) => n + 1);
  };

  /** Again, live: a follow-up again of the run it followed (its thread is the seed until the server's arrives). */
  const again = (inv: Investigation) => {
    if (inv.followUpOf) {
      const parent = inv.context[inv.context.length - 1];
      askFollowUp(inv.question, { runId: inv.followUpOf, question: parent?.question ?? '', context: inv.context, until: null });
    } else {
      start(inv.question, { live: true });
    }
  };

  const toggleMic = () => {
    setNote(null);
    if (dict.listening) dict.stop();
    else {
      dict.clearError();
      dict.start(draft);
    }
  };

  useFollow(body, iv.inv, `${idp}-answer`);

  /** True the first time a link's words are seen: one link is acted on once. */
  const claimLink = useCallback((key: string) => {
    if (urlSeen.current === key) return false;
    urlSeen.current = key;
    return true;
  }, []);
  /** The address no longer carries a link's words: the same link followed again is a new request.
   *  (/live and /live?draft=… are one page instance in Next 16, so this console outlives the visit.) */
  const releaseLink = useCallback(() => {
    urlSeen.current = null;
  }, []);

  const fromUrl = (
    <Suspense fallback={null}>
      <FromUrl claim={claimLink} release={releaseLink} onAsk={setLinked} onDraft={prefill} />
    </Suspense>
  );

  if (collapsed) {
    return (
      <section className="iv" data-collapsed="" aria-label="Investigator">
        {fromUrl}
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
  const describedBy = [`${idp}-budget`, dict.listening || dict.error ? `${idp}-voice` : null, note ? `${idp}-note` : null].filter(Boolean).join(' ');

  return (
    <section ref={root} className="iv lv-card" id={`${idp}-console`} aria-labelledby={`${idp}-title`}>
      {fromUrl}
      <header className="iv-head">
        <h2 className="lv-cardTitle" id={`${idp}-title`}>
          Investigator
        </h2>
        <BudgetPill budget={budget} loading={!status.data && !status.error} offline={offline} />
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
            offline={offline}
            followingThis={Boolean(followTo && followTo.runId === inv.runId)}
            questionRef={questionRef}
            onBack={back}
            onStop={iv.stop}
            onSkip={iv.skip}
            onLive={() => again(inv)}
            onReplay={() => inv.runId && void iv.play(inv.runId, inv.question)}
            onReapply={iv.reapply}
            onOpenCase={iv.openCase}
            onFollowUp={() => followUp(inv)}
            onEdit={() => setFocusBox((n) => n + 1)}
          />
        ) : (
          <Start
            idp={idp}
            examples={examples}
            history={iv.history}
            onExample={(ex) => {
              setCollapsed(false);
              setFollowTo(null);
              void iv.play(ex.id, ex.question);
              requestAnimationFrame(() => questionRef.current?.focus({ preventScroll: true }));
            }}
            onHistory={(h) => {
              setFollowTo(null);
              if (h.runId) void iv.play(h.runId, h.question, { paced: false });
              else if (h.followUpOf) askFollowUp(h.question, { runId: h.followUpOf, question: '', context: [], until: null });
              else start(h.question, { live: true, focus: true });
              requestAnimationFrame(() => questionRef.current?.focus({ preventScroll: true }));
            }}
          />
        )}
      </div>

      <form className="iv-composer" onSubmit={onSubmit} role="search" aria-label="Ask the investigator">
        {followTo ? (
          <div className="iv-followTo" data-closed={followClosed ? '' : undefined}>
            <p className="iv-followToText" role={followClosed ? 'status' : undefined}>
              <FollowMark className="iv-followToMark" />
              {followClosed ? (
                <span>{FOLLOW_UP_TOO_OLD}</span>
              ) : (
                <span>
                  Follow-up to <q>{followTo.question}</q>
                </span>
              )}
            </p>
            <button
              type="button"
              className="iv-followCancel"
              onClick={() => {
                setFollowTo(null);
                inputRef.current?.focus({ preventScroll: true });
              }}
              aria-label={followClosed ? 'Ask it as a question on its own' : 'Cancel the follow-up: ask a question on its own'}
            >
              {followClosed ? 'Ask on its own' : 'Cancel'}
            </button>
          </div>
        ) : null}
        <label htmlFor={`${idp}-q`} className="sr-only">
          {followTo ? 'Your follow-up question' : 'Your question'}
        </label>
        <div className="iv-askRow">
          <textarea
            ref={inputRef}
            id={`${idp}-q`}
            className="iv-input"
            name="question"
            rows={1}
            autoComplete="off"
            enterKeyHint="send"
            maxLength={MAX_QUESTION}
            placeholder={dict.listening ? 'Listening…' : followTo ? 'Your follow-up, e.g. And in Haryana?' : 'Ask about fires, burn scars, smoke or air…'}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setNote(null);
              // typing takes over from the microphone
              if (dict.listening) dict.abort();
            }}
            onKeyDown={(e) => {
              // Enter sends, as a one-line box would; Shift+Enter breaks the line. Never while an input
              // method is composing a word (Hindi and Punjabi are often typed through one): Safari
              // fires the Enter that confirms the word after compositionend, with isComposing false
              // but keyCode 229, so that marker counts too.
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
            aria-describedby={describedBy}
          />
          {dict.supported ? <MicButton dict={dict} disabled={offline} describedBy={dict.listening || dict.error ? `${idp}-voice` : undefined} onClick={toggleMic} /> : null}
          {running && !draft.trim() ? (
            <button type="button" className="iv-button" data-variant="stop" onClick={iv.stop}>
              Stop
            </button>
          ) : (
            <button type="submit" className="iv-button" disabled={askBlocked} title={askBlocked ? 'A new question needs a connection' : undefined}>
              Ask
            </button>
          )}
        </div>
        <VoiceRow dict={dict} draft={draft} id={`${idp}-voice`} />
        {note ? (
          <p className="iv-composerNote" id={`${idp}-note`} role="status">
            {note}
          </p>
        ) : null}
        <BudgetLine id={`${idp}-budget`} budget={budget} error={!!status.error && !status.data} offline={offline} />
      </form>
    </section>
  );
}

/**
 * Reads `?ask=` and `?draft=` once, hands them over and takes them off the address, so a reload does
 * not ask again. `claim` is the console's: the copy in the collapsed and in the open console never
 * both act on one link. Once the address is clean again the claim is released, so following the same
 * link later in the visit (a brief's "Ask the investigator", Back, the same link) fills the box again.
 */
function FromUrl({
  claim,
  release,
  onAsk,
  onDraft,
}: {
  claim: (key: string) => boolean;
  release: () => void;
  onAsk: (q: string) => void;
  onDraft: (q: string) => void;
}) {
  const params = useSearchParams();
  const ask = params.get('ask');
  const draft = params.get('draft');
  useEffect(() => {
    if (!ask && !draft) {
      release();
      return;
    }
    if (!claim(`${ask ?? ''}\n${draft ?? ''}`)) return;
    if (ask) onAsk(ask);
    if (draft) onDraft(draft);
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('ask');
      url.searchParams.delete('draft');
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    } catch {
      // leave the address as it is
    }
  }, [ask, draft, claim, release, onAsk, onDraft]);
  return null;
}

// ---------------------------------------------------------------------------------------------
// before a question: the guardrails, the verified questions and this session's

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
  // curated for display: no raw typing, one of each near-duplicate pair (data.ts); once per list,
  // not on every tick of the console's clock
  const list = useMemo(() => curateExamples(examples.data?.examples ?? []), [examples.data]);
  return (
    <>
      <p className="iv-intro">
        Gemini answers from BlindSpot&rsquo;s live data only. You see each tool it calls, the map follows what it finds, and every figure in the answer is cited and checked against the data
        before you read it.
      </p>
      <Guardrails />
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
                  {/* a share of candidate unseen burns never shows without its audit */}
                  {ex.answer && /unseen/i.test(firstSentence(ex.answer)) ? <Precision inline /> : null}
                  <span className="iv-exMeta">
                    {[
                      ex.verification ? checkedWords(ex.verification.verified, ex.verification.checked) : null,
                      ex.tools_used?.length ? toolCount(ex.tools_used.filter((t) => t !== 'show_on_map').length) : null,
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
                      h.followUpOf ? 'follow-up' : null,
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

/**
 * The guardrails' record over 30 days (GET /api/agent/stats), counted from the stored runs, never
 * from the model: how many figures matched the data, how many drafts were corrected or rewritten.
 * Gone, not an error, when the counts cannot be read: the intro above stands on its own.
 */
function Guardrails() {
  const st = useAgentStats();
  if (!st.data) {
    if (st.error) return null;
    return (
      <div className="iv-proof" aria-hidden="true">
        <Skeleton width={14} height={14} />
        <span className="iv-proofSkeleton">
          <Skeleton width="96%" height={12} />
          <Skeleton width="64%" height={12} />
        </span>
      </div>
    );
  }
  const words = guardrailWords(st.data);
  if (!words) return null;
  return (
    <p className="iv-proof">
      <svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" className="iv-proofMark">
        <path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span>
        <span className="iv-proofLead">Checked before you read it:</span> {words}{' '}
        <Link href="/method#investigator" className="iv-proofLink">
          How the check works
        </Link>
      </span>
    </p>
  );
}

/** The verdict line of a stored answer, without its citation markers (they are linked in the run itself). */
const firstSentence = (text: string) =>
  plainText(text.split(/\n/)[0])
    // the model's own label ("Verdict: Tarn Taran and …") is dropped; the verdict itself stays
    .replace(/^(?:verdict|answer|summary)\s*:\s*/i, '');

const toolCount = (n: number) => `${number(n)} ${n === 1 ? 'tool' : 'tools'}`;

const checkedWords = (verified: number, checked: number) =>
  checked === 0 ? 'no figures to check' : verified === checked ? `${number(checked)} of ${number(checked)} figures checked` : `${number(verified)} of ${number(checked)} figures matched`;

// ---------------------------------------------------------------------------------------------
// one question

function ModeLine({ inv, cacheMinutes, now }: { inv: Investigation; cacheMinutes: number | null; now: Date | null }) {
  const running = inv.status === 'running';
  if (inv.error?.refused) {
    return (
      <p className="iv-mode" data-mode="refused">
        Not asked: the server turned it down before any run started, so no Gemini call was made.
      </p>
    );
  }
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

/** The earlier questions and answers a follow-up was given, folded above it: the thread it continues.
 *  (A follow-up re-asked from this session's list has none until its `run` event brings them.) */
function Thread({ inv, idp }: { inv: Investigation; idp: string }) {
  if (!inv.context.length) return null;
  return (
    <section className="iv-thread" aria-labelledby={`${idp}-thread`}>
      <h4 className="iv-threadHead" id={`${idp}-thread`}>
        Earlier in this conversation
      </h4>
      <ol className="iv-threadList">
        {inv.context.map((p, i) => (
          <li key={`${p.runId}-${i}`}>
            <details className="iv-ctx">
              <summary>
                <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" className="iv-ctxChevron">
                  <path d="M4.5 2.5L8 6 4.5 9.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <span className="iv-ctxQ">{p.question}</span>
              </summary>
              <p className="iv-ctxA">{p.answer || 'Its answer text was not kept.'}</p>
            </details>
          </li>
        ))}
      </ol>
      <p className="iv-threadNote">Gemini is given these as context, not as evidence: every figure in the follow-up&rsquo;s answer is fetched and checked again.</p>
    </section>
  );
}

function Transcript({
  inv,
  idp,
  budget,
  cacheMinutes,
  now,
  offline,
  followingThis,
  questionRef,
  onBack,
  onStop,
  onSkip,
  onLive,
  onReplay,
  onReapply,
  onOpenCase,
  onFollowUp,
  onEdit,
}: {
  inv: Investigation;
  idp: string;
  budget: Budget;
  cacheMinutes: number | null;
  now: Date | null;
  offline: boolean;
  /** the box is writing a follow-up to this run */
  followingThis: boolean;
  questionRef: React.RefObject<HTMLHeadingElement | null>;
  onBack: () => void;
  onStop: () => void;
  onSkip: () => void;
  onLive: () => void;
  onReplay: () => void;
  onReapply: (action: string, payload: Record<string, unknown>) => boolean;
  onOpenCase: (id: number) => void;
  onFollowUp: () => void;
  onEdit: () => void;
}) {
  const running = inv.status === 'running';
  const evidenceOnly = budget.state === 'spent' || budget.state === 'off';
  const followable = canFollowUp(inv, now?.getTime() ?? null);
  const needsNet = offline ? 'Needs a connection' : undefined;
  return (
    <article className="iv-run" aria-labelledby={`${idp}-question`}>
      <button type="button" className="iv-back lv-linkish" onClick={onBack}>
        <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14">
          <path d="M8.5 3.5L5 7l3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        All questions
      </button>
      <Thread inv={inv} idp={idp} />
      {inv.followUpOf ? <p className="iv-kicker">Follow-up</p> : null}
      <h3 className="iv-question" id={`${idp}-question`} ref={questionRef} tabIndex={-1} data-follow-up={inv.followUpOf ? '' : undefined}>
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
          <button
            type="button"
            className="lv-ctrl"
            onClick={onLive}
            disabled={offline}
            title={needsNet ?? (evidenceOnly ? 'Today\'s Gemini budget is spent: a live question gets the evidence without a written answer.' : undefined)}
          >
            Ask it again live
          </button>
        ) : null}
        {/* a playback that ended early (stopped, or cut off when the visitor left mission control) plays again too */}
        {!running && inv.mode === 'stored' && inv.runId && (inv.status === 'done' || inv.status === 'stopped') ? (
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
            <button
              type="button"
              className="lv-ctrl"
              onClick={inv.mode === 'stored' && inv.runId && !inv.steps.length ? onReplay : onLive}
              disabled={offline && !(inv.mode === 'stored' && inv.runId)}
              title={offline && !(inv.mode === 'stored' && inv.runId) ? needsNet : undefined}
            >
              Try again
            </button>
          ) : inv.error.refused ? (
            // refused before it started: the words are back in the box, to rewrite
            <button type="button" className="lv-ctrl" onClick={onEdit}>
              Edit the question
            </button>
          ) : null}
        </div>
      ) : null}
      {inv.status === 'stopped' ? (
        <p className="iv-note" role="status">
          Stopped.{' '}
          {inv.mode === 'live'
            ? 'The run carries on on the server and is stored; ask the same question within a few minutes to see its answer without a new call.'
            : inv.mode === 'stored'
              ? 'Play it again to see it through to the answer.'
              : ''}
        </p>
      ) : null}
      {inv.recovering ? (
        <p className="iv-note iv-recovering" role="status">
          <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14" className="iv-spin">
            <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeOpacity="0.3" strokeWidth="1.5" />
            <path d="M7 2.5a4.5 4.5 0 014.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          The connection dropped, but the run carries on on the server. Loading its stored, checked answer…
        </p>
      ) : null}

      <AnswerPanel inv={inv} idp={idp} onOpenCase={onOpenCase} />

      {followable ? (
        <div className="iv-next">
          <button type="button" className="iv-followButton" onClick={onFollowUp} disabled={offline} aria-describedby={`${idp}-followNote`} data-active={followingThis ? '' : undefined}>
            <FollowMark />
            Ask a follow-up
          </button>
          <p className="iv-followNote" id={`${idp}-followNote`}>
            {offline
              ? 'A follow-up needs a connection.'
              : followingThis
                ? 'Write it in the box below.'
                : `It keeps this question and answer as context; every figure is fetched and checked again. Open until ${dateTimeIST(inv.followUpUntil, { suffix: true })}.`}
          </p>
        </div>
      ) : null}
    </article>
  );
}

/** The follow-up arrow: on the button that starts one, and on the box while one is written. */
function FollowMark({ className }: { className?: string }) {
  return (
    <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14" className={className}>
      <path d="M3 2.5v4.2c0 1.4 1.1 2.5 2.5 2.5H11M8.6 6.8L11 9.2l-2.4 2.4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ---------------------------------------------------------------------------------------------
// speaking a question (where the browser can listen)

function MicButton({ dict, disabled, describedBy, onClick }: { dict: Dictation; disabled: boolean; describedBy?: string; onClick: () => void }) {
  return (
    <button
      type="button"
      className="iv-mic"
      data-listening={dict.listening ? '' : undefined}
      aria-pressed={dict.listening}
      aria-label="Speak your question"
      aria-describedby={describedBy}
      title={disabled ? 'Speaking a question needs a connection' : dict.listening ? 'Stop listening' : 'Speak your question'}
      disabled={disabled}
      onClick={onClick}
    >
      {dict.listening ? (
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18">
          <rect x="5" y="5" width="8" height="8" rx="1.5" fill="currentColor" />
        </svg>
      ) : (
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18">
          <rect x="6.5" y="2.5" width="5" height="8.5" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M4 8.5a5 5 0 0010 0M9 13.5V16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      )}
    </button>
  );
}

/** While listening (or after it failed): what is happening, and the language it listens in. */
function VoiceRow({ dict, draft, id }: { dict: Dictation; draft: string; id: string }) {
  if (!dict.supported || (!dict.listening && !dict.error)) return null;
  const lang = dict.lang ?? 'en';
  return (
    <div className="iv-voice" data-state={dict.listening ? 'listening' : 'error'}>
      <p className="iv-voiceText" id={id} role="status">
        {dict.listening ? (
          <>
            <span className="iv-recDot" aria-hidden="true" />
            Listening in {langEnglish(lang)}: speak your question, then press Ask.
          </>
        ) : (
          dict.error
        )}
      </p>
      <div className="iv-voiceLangs" role="group" aria-label="The language you speak in">
        {LANGS.map((l) => (
          <button key={l.code} type="button" className="iv-voiceLang" lang={l.code} aria-pressed={lang === l.code} onClick={() => dict.switchLang(l.code, draft)}>
            {l.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// the Gemini budget (GET /api/agent/status)

function BudgetPill({ budget, loading, offline }: { budget: Budget; loading: boolean; offline: boolean }) {
  if (offline) return <span className="lv-pill iv-pill" data-tone="warning">Offline</span>;
  if (loading) return <Skeleton width={84} height={20} style={{ marginLeft: 'auto' }} />;
  if (budget.state === 'spent') return <span className="lv-pill iv-pill" data-tone="warning">Live budget spent</span>;
  if (budget.state === 'cooling') return <span className="lv-pill iv-pill" data-tone="warning">Gemini cooling down</span>;
  if (budget.state === 'off') return <span className="lv-pill iv-pill" data-tone="warning">Live answers off</span>;
  if (budget.state === 'open') return <span className="lv-pill iv-pill">Live answers on</span>;
  return <span className="iv-pill" />;
}

function BudgetLine({ id, budget, error, offline }: { id: string; budget: Budget; error: boolean; offline: boolean }) {
  let text: string;
  let tone: 'warning' | undefined;
  if (offline) {
    // checked first: the budget cannot be read offline either, and "may still run live" would be untrue
    text = 'No connection: a new question needs one. The verified questions this browser has already played still play from its saved copies.';
    tone = 'warning';
  } else if (error) text = 'The investigator\'s budget could not be read; a question may still run live.';
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
