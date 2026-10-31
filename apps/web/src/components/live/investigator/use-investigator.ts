'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { useMapCommands } from '@/components/live/map-commands';
import { streamAgent } from '@/lib/sse';

import { loadRun, type StoredRun } from './data';
import { evidenceOf, newInvestigation, reduce, type Investigation, type RawEvent, type RunMode, type StoredTool } from './model';

/**
 * Runs the console: asks live (POST /api/agent/ask, streamed), plays a stored verified run
 * (GET /api/agent/runs/{id}), drives the map from the run's `map` events and the evidence it named,
 * and keeps the session's questions.
 *
 *   const iv = useInvestigator();
 *   iv.ask('Why is Delhi\'s air bad today?');   // live
 *   iv.play(example.id);                          // a stored run, no Gemini call
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

const prefersReducedMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;

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
    (question: string, mode: RunMode) => {
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      fast.current = false;
      const fresh = newInvestigation(question, mode, Date.now());
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
      }
      update((s) => (s.key === key ? reduce(s, ev, clock, applied) : s));
    },
    [map, update],
  );

  /** After the run: outline on the map the fields and fires its tools returned. */
  const showEvidence = useCallback(
    (tools: StoredTool[] | null) => {
      const ev = evidenceOf(tools);
      // Outlined, never framed: these are the tool's sample of fields, often not the districts the
      // answer names (a season answer about Amritsar framed eight fields in Sangrur). The camera
      // moves only where the agent itself sent it (show_on_map), and the answer lists the fields.
      if (ev.fireIds.length) map.highlight('fire', ev.fireIds, { replace: true });
      if (ev.scars.length) map.highlight('scar', ev.scars.map((x) => x.id), { replace: true, focus: false });
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
      };
      const next = [item, ...h.filter((x) => x.key !== s.key && !(x.runId && x.runId === s.runId))].slice(0, 20);
      writeHistory(next);
      return next;
    });
  }, []);

  /** Ask live. The server may still replay its own answer of the last few minutes (mode 'cached'). */
  const ask = useCallback(
    async (question: string) => {
      const q = question.replace(/\s+/g, ' ').trim();
      if (!q) return;
      const { c, key } = begin(q, 'live');
      for await (const ev of streamAgent({ question: q }, { signal: c.signal })) {
        if (c.signal.aborted) return;
        if (ev.type === 'unknown') continue;
        feed(key, ev as unknown as RawEvent, performance.now());
      }
      if (c.signal.aborted) return;
      update((s) => (s.key === key && s.status === 'running' ? { ...s, status: 'done', endedAt: Date.now() } : s));
      const done = current.current;
      if (!done || done.key !== key) return;
      remember(done);
      if (!done.runId) return;
      // The stream does not carry tool outputs; the stored run has them (it is saved before the stream ends).
      try {
        const res = await loadRun(done.runId, c.signal);
        if (c.signal.aborted) return;
        update((s) => (s.key === key ? { ...s, tools: res.run.tools ?? [] } : s));
        showEvidence(res.run.tools ?? []);
      } catch {
        // the evidence list is extra: the answer and its sources are already on screen
      }
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
        update((s) =>
          s.key === key
            ? { ...s, status: 'error', runId, error: { message: "Couldn't load this run: the BlindSpot API did not answer.", retryable: true }, endedAt: Date.now() }
            : s,
        );
        return;
      }
      if (c.signal.aborted) return;
      update((s) =>
        s.key === key
          ? { ...s, question: run.question || s.question, runId: run.id, narratedAt: run.started_at, durationMs: run.duration_ms ?? null, tools: run.tools ?? [] }
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
      showEvidence(run.tools ?? []);
    },
    [begin, feed, pause, remember, showEvidence, update],
  );

  const stop = useCallback(() => {
    ctrl.current?.abort();
    update((s) => (s.status === 'running' ? { ...s, status: 'stopped', endedAt: Date.now() } : s));
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

  useEffect(() => () => ctrl.current?.abort(), []);

  return { inv, history, ask, play, stop, skip, close, reapply, openCase: map.openCase };
}
