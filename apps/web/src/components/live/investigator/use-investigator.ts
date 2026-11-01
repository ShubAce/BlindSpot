'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { useMapCommands } from '@/components/live/map-commands';
import { prefersReducedMotion } from '@/components/map/clock';
import { connectivityNow, isOffline } from '@/components/pwa/connectivity';
import { streamAgent } from '@/lib/sse';

import { loadRun, type StoredRun, type StoredRunResponse } from './data';
import {
  evidenceOf,
  localError,
  movedCamera,
  newInvestigation,
  reduce,
  threadFromStored,
  type FollowUp,
  type Investigation,
  type RawEvent,
  type RunError,
  type RunMode,
  type StoredTool,
} from './model';

/**
 * Runs the console: asks live (POST /api/agent/ask, streamed), asks a follow-up of an answered run
 * (the same, with follow_up_of), plays a stored verified run (GET /api/agent/runs/{id}), drives the
 * map from the run's `map` events and the evidence it named, and keeps the session's questions.
 *
 *   const iv = useInvestigator();
 *   iv.ask('Why is Delhi\'s air bad today?');                 // live
 *   iv.ask('And in Haryana?', { followUp: followUpOf(inv) });   // a follow-up of an answered run
 *   iv.play(example.id);                                        // a stored run, no Gemini call
 *   iv.stop();  iv.skip();  iv.close();
 */

export type HistoryItem = {
  key: string;
  question: string;
  mode: RunMode;
  runId: string | null;
  /** UTC ISO, when it was asked in this session */
  at: string;
  status: Investigation['status'];
  verified: { checked: number; verified: number; passed: boolean } | null;
  /** the run it followed up, if it was a follow-up */
  followUpOf?: string | null;
};

export type AskOptions = {
  /** Ask it as a follow-up of this thread (sent as follow_up_of). */
  followUp?: FollowUp | null;
  /** Called when the question was never asked: the server refused it before starting a run (a
   *  400, 422, 429 or 503), or could not be reached. The console hands the words back to the box. */
  onNotAsked?: (error: RunError) => void;
};

const HISTORY_KEY = 'blindspot:investigator:history';

/** Pace of a stored run played back (ms before each event): close to how a live run unfolds. */
const PACE: Record<string, number> = {
  run: 250,
  thought: 450,
  tool_call: 300,
  map: 250,
  tool_result: 550,
  answer_delta: 70,
  answer: 300,
  verification: 450,
  error: 200,
  done: 0,
};

function readHistory(): HistoryItem[] {
  try {
    const raw = window.sessionStorage.getItem(HISTORY_KEY);
    const v = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(v) ? (v as HistoryItem[]).slice(0, 20) : [];
  } catch {
    return [];
  }
}
function writeHistory(items: HistoryItem[]) {
  try {
    window.sessionStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 20)));
  } catch {
    // private mode or storage blocked: the history lives for this page only
  }
}

/** A wait that ends at once, leaving no timer behind, when `signal` aborts. */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(t);
      signal.removeEventListener('abort', done);
      resolve();
    };
    const t = setTimeout(done, ms);
    signal.addEventListener('abort', done, { once: true });
  });
}

/**
 * A stored run (GET /api/agent/runs/{id}), read until `accept` takes it: at most `tries` reads, and
 * none after `forMs`, `gapMs` apart. A failed read (404 until the run is stored, or the network still
 * out) is asked again. Null when no read was taken, or `signal` aborted.
 */
async function pollRun(
  runId: string,
  signal: AbortSignal,
  opts: { gapMs: number; tries?: number; forMs?: number; accept?: (r: StoredRunResponse) => boolean },
): Promise<StoredRunResponse | null> {
  const until = opts.forMs != null ? Date.now() + opts.forMs : Infinity;
  for (let tried = 1; ; tried++) {
    try {
      const res = await loadRun(runId, signal);
      if (!opts.accept || opts.accept(res)) return res;
    } catch {
      // not stored yet, or not reachable: ask again
    }
    if (signal.aborted || tried >= (opts.tries ?? Infinity)) return null;
    await sleep(opts.gapMs, signal);
    if (signal.aborted || Date.now() >= until) return null;
  }
}

/** The stored copy of a run just answered, tried twice: it is saved as the stream ends, and a cold
 *  API instance may not have it on the first read. */
const loadStored = (runId: string, signal: AbortSignal) => pollRun(runId, signal, { tries: 2, gapMs: 1_500 });

/** A run whose stream broke, as the server stores it once it finishes (404 until then): asked every
 *  2.5 s for up to 75 s, longer than any investigation takes. */
const waitStored = (runId: string, signal: AbortSignal) =>
  pollRun(runId, signal, { forMs: 75_000, gapMs: 2_500, accept: (res) => Boolean(res.run?.events?.length) });

export function useInvestigator() {
  const map = useMapCommands();
  const [inv, setInv] = useState<Investigation | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const ctrl = useRef<AbortController | null>(null);
  const fast = useRef(false);
  const wake = useRef<(() => void) | null>(null);
  const current = useRef<Investigation | null>(null);

  useEffect(() => {
    // sessionStorage is only readable in the browser; the history fills in after hydration
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHistory(readHistory());
  }, []);

  /** Every change goes through the ref first, so code after an await reads the newest state. */
  const update = useCallback((fn: (s: Investigation) => Investigation) => {
    const s = current.current;
    if (!s) return;
    const next = fn(s);
    if (next === s) return;
    current.current = next;
    setInv(next);
  }, []);

  const begin = useCallback(
    (question: string, mode: RunMode, followUp?: FollowUp | null) => {
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      fast.current = false;
      const fresh = newInvestigation(question, mode, Date.now(), followUp);
      current.current = fresh;
      setInv(fresh);
      map.clearHighlights();
      return { c, key: fresh.key };
    },
    [map],
  );

  /** Side effects of one event on the map, then the event into state. */
  const feed = useCallback(
    (key: string, ev: RawEvent, clock: number | null) => {
      let applied = false;
      if (ev.type === 'map') {
        applied = map.applyAgentEvent({ action: String(ev.action ?? ''), payload: (ev.payload ?? {}) as Record<string, unknown> });
      } else if (ev.type === 'tool_call' && ev.name === 'get_case_file') {
        const id = Number((ev.args as Record<string, unknown> | undefined)?.scar_id);
        if (Number.isInteger(id) && id > 0) map.openCase(id, { reveal: false });
      } else if (ev.type === 'tool_call' && ev.name === 'get_field_brief') {
        // A field brief is about one district's candidate unseen burns: their layer goes on while it
        // reads (the API sends no map hint for this tool), and its fields are outlined once it ends.
        map.showLayers(['scars'], { reveal: false });
      }
      update((s) => (s.key === key ? reduce(s, ev, clock, applied) : s));
    },
    [map, update],
  );

  /** After the run: outline on the map the fields and fires its tools returned. */
  const showEvidence = useCallback(
    (tools: StoredTool[] | null, inv: Investigation | null) => {
      const ev = evidenceOf(tools);
      // Outlined, not framed: a get_burn_scars answer's fields are the tool's sample, often not the
      // districts the answer names (a season answer about Amritsar framed eight fields in Sangrur).
      // The camera moves where the agent itself sent it (show_on_map). A field brief is the one
      // exception: its fields are the district the answer is about, so when the run moved the
      // camera nowhere, they are framed.
      if (ev.fireIds.length) map.highlight('fire', ev.fireIds, { replace: true });
      if (ev.scars.length) map.highlight('scar', ev.scars.map((x) => x.id), { replace: true, focus: ev.briefOnly && !movedCamera(inv) });
    },
    [map],
  );

  const remember = useCallback((s: Investigation) => {
    setHistory((h) => {
      const item: HistoryItem = {
        key: s.key,
        question: s.question,
        mode: s.mode,
        runId: s.runId,
        at: new Date(s.startedAt).toISOString(),
        status: s.status,
        verified: s.verification ? { checked: s.verification.checked, verified: s.verification.verified, passed: s.verification.passed } : null,
        followUpOf: s.followUpOf,
      };
      const next = [item, ...h.filter((x) => x.key !== s.key && !(x.runId && x.runId === s.runId))].slice(0, 20);
      writeHistory(next);
      return next;
    });
  }, []);


  /**
   * Ask live; with `followUp`, as a follow-up of that answered run. The server may still replay its
   * own answer of the last few minutes (mode 'cached'). If the connection breaks once the run has
   * begun, the run carries on and is stored: its stored copy finishes the transcript, so a dropped
   * connection never leaves an unchecked draft standing in for the answer.
   */
  const ask = useCallback(
    async (question: string, opts: AskOptions = {}) => {
      const q = question.replace(/\s+/g, ' ').trim();
      if (!q) return;
      const followUp = opts.followUp ?? null;
      const { c, key } = begin(q, 'live', followUp);
      let started = false;
      /** the server's events this stream delivered, in order (a stored run holds the same sequence) */
      let received = 0;
      let broken = false;
      /** `done` arrived: the answer and its check are complete, whatever happens to the stream after */
      let doneSeen = false;
      for await (const ev of streamAgent(followUp ? { question: q, follow_up_of: followUp.runId } : { question: q }, { signal: c.signal })) {
        if (c.signal.aborted) return;
        if (ev.type === 'error' && ev.transport && started) {
          // The server sends `done`, then stores the run, then closes the stream: a reset in that
          // window loses nothing, so it is no reason to show the recovery note over a checked answer
          // (the stored copy read below still brings the tools and the follow-up window).
          if (!doneSeen) broken = true;
          break;
        }
        if (!(ev.type === 'error' && ev.transport)) received++;
        if (ev.type === 'unknown') continue;
        if (ev.type === 'run') started = true;
        if (ev.type === 'done') doneSeen = true;
        feed(key, ev as unknown as RawEvent, performance.now());
        // refused (or unreachable) before any run began: nothing was asked
        if (ev.type === 'error' && !started) {
          const err = current.current?.key === key ? current.current.error : null;
          if (err) opts.onNotAsked?.(err);
        }
      }
      if (c.signal.aborted) return;
      let stored: StoredRunResponse | null = null;
      const runId = current.current?.key === key ? current.current.runId : null;
      if (broken && runId) {
        update((s) => (s.key === key ? { ...s, recovering: true } : s));
        stored = await waitStored(runId, c.signal);
        if (c.signal.aborted) return;
        if (stored) {
          // the events the connection lost, from where it broke: the same reducer, the same map moves
          for (const ev of stored.run.events.slice(received)) feed(key, ev, null);
          update((s) => (s.key === key ? { ...s, recovering: false, durationMs: stored?.run.duration_ms ?? s.durationMs } : s));
        } else {
          update((s) =>
            s.key === key
              ? {
                  ...s,
                  recovering: false,
                  status: 'error',
                  endedAt: Date.now(),
                  error: localError(
                    'The connection dropped, and the run’s stored answer has not arrived. Try again: the same question within 10 minutes replays that answer without a new Gemini call.',
                  ),
                }
              : s,
          );
        }
      }
      update((s) => (s.key === key && s.status === 'running' ? { ...s, status: 'done', endedAt: Date.now() } : s));
      const done = current.current;
      if (!done || done.key !== key) return;
      remember(done);
      if (!done.runId) return;
      // The stream carries no tool outputs and no follow-up window; the stored run has both.
      const res = stored ?? (await loadStored(done.runId, c.signal));
      if (!res || c.signal.aborted) return; // the answer and its sources are on screen already
      const run = res.run;
      update((s) => (s.key === key ? { ...s, tools: run.tools ?? [], ...threadFromStored(run, s) } : s));
      showEvidence(run.tools ?? [], current.current);
    },
    [begin, feed, remember, showEvidence, update],
  );

  /** Wait before the next event of a playback; `skip()` ends every wait at once. */
  const pause = useCallback((ms: number, signal: AbortSignal) => {
    if (ms <= 0 || fast.current) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(t);
        wake.current = null;
        resolve();
      };
      const t = setTimeout(done, ms);
      wake.current = done;
      signal.addEventListener('abort', done, { once: true });
    });
  }, []);

  /** Play a stored, verified run: every event exactly as the API recorded it, paced so it can be followed. */
  const play = useCallback(
    async (runId: string, question = '', opts: { paced?: boolean } = {}) => {
      const { c, key } = begin(question, 'stored');
      if (opts.paced === false || prefersReducedMotion()) fast.current = true;
      let run: StoredRun;
      try {
        run = (await loadRun(runId, c.signal)).run;
      } catch {
        if (c.signal.aborted) return;
        // offline (the console's own test, so Wi-Fi with no internet counts), a run plays only if
        // this browser saved it when it last played (public/sw.js)
        const message = isOffline(connectivityNow())
          ? 'No connection, and this run is not saved in this browser yet: it plays once you are back online.'
          : "Couldn't load this run: the BlindSpot API did not answer.";
        update((s) => (s.key === key ? { ...s, status: 'error', runId, error: localError(message), endedAt: Date.now() } : s));
        return;
      }
      if (c.signal.aborted) return;
      update((s) =>
        s.key === key
          ? {
              ...s,
              question: run.question || s.question,
              runId: run.id,
              narratedAt: run.started_at,
              durationMs: run.duration_ms ?? null,
              tools: run.tools ?? [],
              // the thread as stored: a follow-up shows the answers it was given, and any run its own window
              ...threadFromStored(run, s),
            }
          : s,
      );
      for (const ev of run.events ?? []) {
        await pause(PACE[ev.type] ?? 100, c.signal);
        if (c.signal.aborted) return;
        // `done` of a stored run carries the run's real duration; keep it
        feed(key, ev, null);
      }
      update((s) => (s.key === key && s.status === 'running' ? { ...s, status: 'done', endedAt: Date.now() } : s));
      const done = current.current;
      if (!done || done.key !== key) return;
      remember(done);
      showEvidence(run.tools ?? [], done);
    },
    [begin, feed, pause, remember, showEvidence, update],
  );

  const stop = useCallback(() => {
    ctrl.current?.abort();
    update((s) => (s.status === 'running' ? { ...s, status: 'stopped', endedAt: Date.now(), recovering: false } : s));
    if (current.current?.status === 'stopped') remember(current.current);
  }, [remember, update]);

  /** Show the rest of a playback at once. */
  const skip = useCallback(() => {
    fast.current = true;
    wake.current?.();
  }, []);

  /** Back to the question list (a running live question keeps going on the server and is stored). */
  const close = useCallback(() => {
    ctrl.current?.abort();
    current.current = null;
    setInv(null);
  }, []);

  /** Re-apply one map event of the run (the "Show again" button on a step). */
  const reapply = useCallback((action: string, payload: Record<string, unknown>) => map.applyAgentEvent({ action, payload }), [map]);

  // Leaving the console ends its run here. Next keeps /live mounted but hidden (React's <Activity>)
  // after a navigation away: that runs this cleanup but keeps the state, and an aborted ask() or
  // play() returns without settling it. So the run is settled here too, as a Stop would: back on
  // /live the console says it stopped (a live run carries on on the server and is stored) instead of
  // a clock still ticking over an unchecked draft. (BaseMap settles the map's state the same way.)
  useEffect(
    () => () => {
      ctrl.current?.abort();
      const s = current.current;
      if (s?.status !== 'running') return;
      const next: Investigation = { ...s, status: 'stopped', endedAt: Date.now(), recovering: false };
      current.current = next;
      setInv(next);
    },
    [],
  );

  return { inv, history, ask, play, stop, skip, close, reapply, openCase: map.openCase };
}
