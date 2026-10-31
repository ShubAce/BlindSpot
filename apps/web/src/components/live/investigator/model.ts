/**
 * The investigation as data: one pure reducer from the API's SSE events (docs/api-contract-notes.md,
 * "Agent and bulletin") to what the console shows. The same reducer reads a live stream, a stream
 * the server replays from its 10-minute answer cache, and a stored run played back in the browser
 * from GET /api/agent/runs/{id}, so all three look alike and differ only in their `mode` line.
 */

import type { SourceRef } from '@/lib/api';

export type RunMode =
  /** a new run: Gemini is answering now */
  | 'live'
  /** the server replayed a run of the same question from the last few minutes */
  | 'cached'
  /** a verified run stored by the API, played back here (no Gemini call) */
  | 'stored';

export type RunStatus = 'running' | 'done' | 'error' | 'stopped';

/** A citation's source: a SourceRef plus the run's marker number. */
export type CiteSource = SourceRef & { marker?: number | string };
export type Cite = { marker: number | string; source: CiteSource };

export type Verification = {
  checked: number;
  verified: number;
  unverified: string[];
  passed: boolean;
  uncited?: string[];
  bad_markers?: (string | number)[];
  revised?: boolean;
  first_pass_unverified?: string[];
};

export type ToolStep = {
  kind: 'tool';
  key: string;
  id: string;
  name: string;
  args: Record<string, unknown>;
  /** performance.now() when the call arrived; null when timing means nothing (a replay) */
  t0: number | null;
  /** the map event this call produced (show_on_map) */
  map?: { action: string; payload: Record<string, unknown>; applied: boolean };
  result?: { ok: boolean; summary: string; sources: CiteSource[]; ms: number | null };
};
export type ThoughtStep = { kind: 'thought'; key: string; texts: string[] };
/** A map event no tool call asked for (the server's own hint: the layer of the main evidence). */
export type MapStep = { kind: 'map'; key: string; action: string; payload: Record<string, unknown>; applied: boolean };
/** The gateway fell back to another model in the middle of the run. */
export type ModelStep = { kind: 'model'; key: string; model: string; from: string | null };
export type Step = ToolStep | ThoughtStep | MapStep | ModelStep;

/** One tool's stored output (GET /api/agent/runs/{id} `run.tools`). */
export type StoredTool = { id: string; name: string; args?: Record<string, unknown>; ok?: boolean; output?: Record<string, unknown> | null };

export type Investigation = {
  key: string;
  question: string;
  mode: RunMode;
  status: RunStatus;
  runId: string | null;
  model: string | null;
  /** UTC ISO: when a replayed answer was first made */
  narratedAt: string | null;
  steps: Step[];
  /** the answer as it streams (the first draft) */
  draft: string;
  /** the final, verified text; may differ from the draft when the verifier asked for a revision */
  answer: { text: string; citations: Cite[] } | null;
  verification: Verification | null;
  error: { message: string; retryable: boolean } | null;
  /** the server's duration_ms from `done` */
  durationMs: number | null;
  models: string[];
  /** Date.now() at start and end, for the elapsed clock of a live run */
  startedAt: number;
  endedAt: number | null;
  /** tool outputs, loaded after the run from GET /api/agent/runs/{id} (the stream does not carry them) */
  tools: StoredTool[] | null;
};

export function newInvestigation(question: string, mode: RunMode, now: number): Investigation {
  return {
    key: `${now.toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
    question,
    mode,
    status: 'running',
    runId: null,
    model: null,
    narratedAt: null,
    steps: [],
    draft: '',
    answer: null,
    verification: null,
    error: null,
    durationMs: null,
    models: [],
    startedAt: now,
    endedAt: null,
    tools: null,
  };
}

/** A raw event as the API sends it (the stream's typed events, or a stored run's JSON). */
export type RawEvent = { type: string; [k: string]: unknown };

const s = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const arr = <T = unknown>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);

let seq = 0;
const k = () => `s${(seq += 1)}`;

/**
 * One event in, the next state out. `clock` is performance.now() for live runs (tool timings) and
 * null for replays. `applied` says whether the map accepted a `map` event.
 */
export function reduce(inv: Investigation, ev: RawEvent, clock: number | null, applied = false): Investigation {
  switch (ev.type) {
    case 'run': {
      const model = s(ev.model);
      const from = s(ev.switched_from);
      const cached = ev.cached === true;
      const next: Investigation = {
        ...inv,
        runId: s(ev.run_id) ?? inv.runId,
        model: model ?? inv.model,
        mode: cached && inv.mode === 'live' ? 'cached' : inv.mode,
        narratedAt: s(ev.narrated_at) ?? inv.narratedAt,
      };
      if (from && model) next.steps = [...inv.steps, { kind: 'model', key: k(), model, from }];
      return next;
    }
    case 'thought': {
      const text = s(ev.text) ?? '';
      if (!text.trim()) return inv;
      const last = inv.steps[inv.steps.length - 1];
      if (last?.kind === 'thought') return { ...inv, steps: [...inv.steps.slice(0, -1), { ...last, texts: [...last.texts, text] }] };
      return { ...inv, steps: [...inv.steps, { kind: 'thought', key: k(), texts: [text] }] };
    }
    case 'tool_call': {
      const id = s(ev.id) ?? k();
      const step: ToolStep = { kind: 'tool', key: k(), id, name: s(ev.name) ?? 'tool', args: obj(ev.args), t0: clock };
      return { ...inv, steps: [...inv.steps, step] };
    }
    case 'map': {
      const action = s(ev.action) ?? '';
      const payload = obj(ev.payload);
      // Attach to the newest show_on_map call that has no map event yet; otherwise it is the
      // server's own hint (payload.auto) and gets a step of its own.
      let idx = -1;
      for (let i = inv.steps.length - 1; i >= 0; i--) {
        const st = inv.steps[i];
        if (st.kind === 'tool' && st.name === 'show_on_map' && !st.map) {
          idx = i;
          break;
        }
      }
      if (idx >= 0 && payload.auto !== true) {
        const steps = inv.steps.slice();
        const st = steps[idx] as ToolStep;
        steps[idx] = { ...st, map: { action, payload, applied } };
        return { ...inv, steps };
      }
      return { ...inv, steps: [...inv.steps, { kind: 'map', key: k(), action, payload, applied }] };
    }
    case 'tool_result': {
      const id = s(ev.id);
      const result = {
        ok: ev.ok !== false,
        summary: s(ev.summary) ?? '',
        sources: arr<CiteSource>(ev.sources),
        ms: null as number | null,
      };
      const i = inv.steps.findIndex((st) => st.kind === 'tool' && st.id === id);
      if (i < 0) {
        return { ...inv, steps: [...inv.steps, { kind: 'tool', key: k(), id: id ?? k(), name: s(ev.name) ?? 'tool', args: {}, t0: null, result }] };
      }
      const steps = inv.steps.slice();
      const st = steps[i] as ToolStep;
      steps[i] = { ...st, result: { ...result, ms: clock != null && st.t0 != null ? clock - st.t0 : null } };
      return { ...inv, steps };
    }
    case 'answer_delta':
      return { ...inv, draft: inv.draft + (s(ev.text) ?? '') };
    case 'answer': {
      const citations = arr<Record<string, unknown>>(ev.citations)
        .map((c) => ({ marker: (n(c.marker) ?? s(c.marker) ?? '') as number | string, source: obj(c.source) as unknown as CiteSource }))
        .filter((c) => c.marker !== '');
      return { ...inv, answer: { text: s(ev.text) ?? inv.draft, citations }, model: s(ev.model) ?? inv.model };
    }
    case 'verification':
      return {
        ...inv,
        verification: {
          checked: n(ev.checked) ?? 0,
          verified: n(ev.verified) ?? 0,
          unverified: arr<string>(ev.unverified).map(String),
          passed: ev.passed === true,
          uncited: arr<string>(ev.uncited).map(String),
          bad_markers: arr<string | number>(ev.bad_markers),
          revised: ev.revised === true,
          first_pass_unverified: arr<string>(ev.first_pass_unverified).map(String),
        },
      };
    case 'error':
      return { ...inv, status: 'error', error: { message: s(ev.message) ?? 'The investigation failed.', retryable: ev.retryable === true } };
    case 'done':
      return {
        ...inv,
        status: inv.status === 'error' ? 'error' : 'done',
        durationMs: n(ev.duration_ms) ?? inv.durationMs,
        models: arr<string>(ev.models).map(String),
        runId: s(ev.run_id) ?? inv.runId,
        endedAt: Date.now(),
      };
    default:
      return inv;
  }
}

// ---------------------------------------------------------------------------------------------
// evidence the answer names, from the stored tool outputs (for the map and the "fields" list)

export type NamedScar = { id: number; village: string | null; district: string | null; areaHa: number | null; unseen: boolean | null };
export type Evidence = { scars: NamedScar[]; fireIds: number[] };

export function evidenceOf(tools: StoredTool[] | null): Evidence {
  const scars = new Map<number, NamedScar>();
  const fires = new Set<number>();
  for (const t of tools ?? []) {
    if (t.ok === false) continue;
    const out = obj(t.output);
    if (t.name === 'get_burn_scars') {
      for (const c of arr<Record<string, unknown>>(out.clusters)) {
        const id = n(c.id);
        if (id == null) continue;
        scars.set(id, {
          id,
          village: s(c.village),
          district: s(c.district),
          areaHa: n(c.area_ha),
          unseen: typeof c.candidate_unseen === 'boolean' ? c.candidate_unseen : null,
        });
      }
    } else if (t.name === 'get_case_file') {
      const c = obj(out.case);
      const id = n(c.id) ?? (typeof t.args?.scar_id === 'string' && /^\d+$/.test(t.args.scar_id) ? Number(t.args.scar_id) : null);
      if (id != null) {
        scars.set(id, { id, village: s(c.village), district: s(c.district), areaHa: n(c.area_ha), unseen: typeof c.unseen === 'boolean' ? c.unseen : null });
      }
    } else if (t.name === 'get_fire_activity') {
      for (const f of arr<Record<string, unknown>>(out.strongest)) {
        const id = n(f.id);
        if (id != null) fires.add(id);
      }
    }
  }
  return { scars: [...scars.values()], fireIds: [...fires] };
}

/** True when a figure about candidate unseen burns is stated (the precision line must go beside it). */
export function mentionsUnseen(inv: Investigation): boolean {
  const re = /unseen/i;
  if (re.test(inv.answer?.text ?? inv.draft)) return true;
  return inv.steps.some((st) => st.kind === 'tool' && re.test(st.result?.summary ?? ''));
}
