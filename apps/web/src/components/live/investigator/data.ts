/**
 * The console's reads: the verified example runs, one stored run, and the Gemini budget
 * (GET /api/agent/examples, /api/agent/runs/{id}, /api/agent/status). Shapes as served on
 * 2026-10-31 (docs/api-contract-notes.md, "Agent and bulletin"); lib/api.ts's older agent types
 * are left alone and these are used here instead.
 */

import { AGENT_STATS_DAYS, apiGet, PATHS, type AgentStats, type SourceRef } from '@/lib/api';
import { number, plural } from '@/lib/format';
import { useApi } from '@/lib/live';
import type { ThreadPairWire } from '@/lib/sse';

import { normaliseQuestion } from './describe';
import type { RawEvent, StoredTool, Verification } from './model';

export type ExampleRun = {
  id: string;
  kind?: string;
  question: string;
  lang?: string | null;
  model?: string | null;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  ok?: boolean;
  answer?: string | null;
  verification?: Verification | null;
  tools_used?: string[] | null;
  /** The thread (additions, 2026-11-01): the run this one followed up, the earlier questions and
   *  answers it was given, and until when it can itself be followed up (UTC; null: never). */
  follow_up_of?: string | null;
  context?: ThreadPairWire[] | null;
  follow_up_until?: string | null;
};
export type ExamplesResponse = { generated_at: string; examples: ExampleRun[]; sources: SourceRef[] };

export type StoredRun = ExampleRun & {
  events: RawEvent[];
  tools: StoredTool[] | null;
  error?: string | null;
};
export type StoredRunResponse = { generated_at: string; run: StoredRun; sources: SourceRef[] };

export type AgentModelStatus = {
  model: string;
  used_today: number;
  daily_limit: number;
  new_questions_stop_at: number;
  cooling_down_until: string | null;
  cooldown_reason?: string | null;
  in_chain?: { ask?: boolean; bulletin?: boolean };
};
export type AgentStatusResponse = {
  generated_at: string;
  key_configured: boolean;
  quota_day_started_at: string | null;
  reserve_share?: number;
  models: AgentModelStatus[];
  per_ip_limits?: { per_minute?: number; per_day?: number };
  answer_cache_minutes?: number;
  sources: SourceRef[];
};

/** More than are shown: curation (below) drops some, and the rest still fill the list. (Server TODO:
 *  curation belongs in the API's examples query, which would let this ask for EXAMPLES_SHOWN.) */
const EXAMPLES_QUERY = { limit: 16 } as const;
/** How many verified questions the console lists. */
export const EXAMPLES_SHOWN = 8;

/** Verified runs: refreshed every few minutes, never on focus (they change only when a run is stored). */
export const useExamples = () =>
  useApi<ExamplesResponse>(PATHS.agentExamples, EXAMPLES_QUERY, { refreshInterval: 5 * 60_000, revalidateOnFocus: false });

// ---------------------------------------------------------------------------------------------
// which verified runs are shown as examples

/**
 * A question as someone typed it in a hurry, not one to show as an example: Latin letters starting
 * in lower case ("how many fires in previous days…"), or no closing question mark or full stop
 * (Hindi and Punjabi end with ? or the danda). Such a run still plays when its words are asked.
 */
export function readsAsRawTyping(question: string): boolean {
  const q = question.trim();
  if (!q) return true;
  const first = q.match(/\p{L}/u)?.[0] ?? '';
  if (/[a-z]/.test(first)) return true;
  return !/[?.।॥]$/.test(q);
}

const STOP = new Set(['the', 'a', 'an', 'of', 'in', 'on', 'at', 'to', 'and', 'or', 'is', 'are', 'was', 'were', 'did', 'do', 'does', 'what', 'which', 'how', 'much', 'many', 'there', 'this', 'that', 'it', 'its', 'had', 'have', 'has', 'any', 'for', 'from', 'with', 'by']);
const content = (q: string) => new Set(normaliseQuestion(q).split(' ').filter((w) => w && !STOP.has(w)));
/** The question's first clause, normalised: "Which districts burned the most in the last 24 hours". */
const lead = (q: string) => normaliseQuestion(q.split(/[,:;?]|\band\b/i)[0] ?? q);

/**
 * Two questions that ask the same thing: the same opening clause (two follow-on halves to "Which
 * districts burned the most in the last 24 hours, and …"), or most of their content words shared.
 */
export function nearDuplicate(a: string, b: string): boolean {
  const la = lead(a);
  if (la.split(' ').length >= 4 && la === lead(b)) return true;
  const A = content(a);
  const B = content(b);
  if (!A.size || !B.size) return false;
  let both = 0;
  for (const w of A) if (B.has(w)) both++;
  return both / (A.size + B.size - both) >= 0.7;
}

/** The better-checked run of two: a passed check first, then more figures matched, then the newer. */
function betterChecked(x: ExampleRun, y: ExampleRun): number {
  const pass = (e: ExampleRun) => (e.verification?.passed ? 1 : 0);
  const matched = (e: ExampleRun) => e.verification?.verified ?? 0;
  return pass(y) - pass(x) || matched(y) - matched(x) || Date.parse(y.started_at) - Date.parse(x.started_at);
}

/**
 * The verified runs worth showing as examples, in the API's order: questions that read like raw,
 * unedited typing are left out, and of near-duplicates only the better-checked one stays. Display
 * only: every stored run still plays when its question is asked.
 */
export function curateExamples(list: readonly ExampleRun[], max = EXAMPLES_SHOWN): ExampleRun[] {
  const candidates = list.filter((e) => !readsAsRawTyping(e.question));
  const kept = new Set<ExampleRun>();
  for (const e of [...candidates].sort(betterChecked)) {
    if ([...kept].some((k) => nearDuplicate(k.question, e.question))) continue;
    kept.add(e);
  }
  return candidates.filter((e) => kept.has(e)).slice(0, max);
}

export const AGENT_STATUS_PATH = '/api/agent/status';
export const useAgentStatus = () => useApi<AgentStatusResponse>(AGENT_STATUS_PATH, undefined, { refreshInterval: 60_000 });

export const loadRun = (id: string, signal?: AbortSignal) => apiGet<StoredRunResponse>(PATHS.agentRun(id), undefined, { signal, timeoutMs: 15_000 });

// ---------------------------------------------------------------------------------------------
// how the guardrails did (GET /api/agent/stats): counted from the stored runs, never from the model

/** The method page reads the same window (AGENT_STATS_DAYS), so both say "the last 30 days" of the same runs. */
const STATS_QUERY = { days: AGENT_STATS_DAYS } as const;

/** Thirty days of counts barely move in an hour (the CDN keeps them an hour too): read rarely, and never on focus. */
export const useAgentStats = () => useApi<AgentStats>(PATHS.agentStats, STATS_QUERY, { refreshInterval: 30 * 60_000, revalidateOnFocus: false });

/**
 * The investigator's guardrails over the window, in one sentence built only from the counts:
 * "293 of 294 figures in 21 answers matched the data (last 30 days); 1 draft corrected, 2 rewritten
 * for honesty." A correction or rewrite that did not then pass says how many did. Null when no
 * answer in the window had a figure to check: then there is nothing to claim.
 */
export function guardrailWords(st: AgentStats): string | null {
  const q = st.questions;
  const f = q?.figures;
  if (!q || !f || !(f.checked > 0) || !(f.runs > 0)) return null;
  const figures =
    f.matched === f.checked ? (f.checked === 1 ? 'the one figure' : `all ${number(f.checked)} figures`) : `${number(f.matched)} of ${plural(f.checked, 'figure', 'figures')}`;
  const extra: string[] = [];
  const c = q.corrected_figures;
  if (c && c.runs > 0) extra.push(`${plural(c.runs, 'draft', 'drafts')} corrected${c.then_passed !== c.runs ? ` (${number(c.then_passed)} then passed)` : ''}`);
  const h = q.honesty;
  if (h && h.first_draft_issues > 0) {
    extra.push(`${number(h.first_draft_issues)} rewritten for honesty${h.then_passed !== h.first_draft_issues ? ` (${number(h.then_passed)} then passed)` : ''}`);
  }
  const days = st.days > 0 ? st.days : AGENT_STATS_DAYS;
  return `${figures} in ${plural(f.runs, 'answer', 'answers')} matched the data (last ${plural(days, 'day', 'days')})${extra.length ? `; ${extra.join(', ')}` : ''}.`;
}

// ---------------------------------------------------------------------------------------------
// the live budget, in words

export type Budget =
  | { state: 'unknown' }
  /** no Gemini key on the server: questions get the evidence without a written answer */
  | { state: 'off' }
  | { state: 'open'; resetsAt: Date | null }
  /** every model in the chain is cooling down after a rate limit */
  | { state: 'cooling'; until: Date; resetsAt: Date | null }
  /** every model in the chain has reached its share for new questions today */
  | { state: 'spent'; resetsAt: Date | null };

export function budgetOf(st: AgentStatusResponse | undefined, now: Date | null): Budget {
  if (!st) return { state: 'unknown' };
  if (!st.key_configured) return { state: 'off' };
  const start = st.quota_day_started_at ? Date.parse(st.quota_day_started_at) : NaN;
  const resetsAt = Number.isFinite(start) ? new Date(start + 86_400_000) : null;
  const chain = st.models.filter((m) => m.in_chain?.ask !== false);
  const withBudget = chain.filter((m) => m.used_today < m.new_questions_stop_at);
  if (!withBudget.length) return { state: 'spent', resetsAt };
  const t = now?.getTime() ?? Date.now();
  const ready = withBudget.filter((m) => !m.cooling_down_until || Date.parse(m.cooling_down_until) <= t);
  if (ready.length) return { state: 'open', resetsAt };
  const until = Math.min(...withBudget.map((m) => Date.parse(m.cooling_down_until as string)));
  return { state: 'cooling', until: new Date(until), resetsAt };
}
