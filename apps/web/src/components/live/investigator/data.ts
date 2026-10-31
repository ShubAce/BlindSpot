/**
 * The console's reads: the verified example runs, one stored run, and the Gemini budget
 * (GET /api/agent/examples, /api/agent/runs/{id}, /api/agent/status). Shapes as served on
 * 2026-10-31 (docs/api-contract-notes.md, "Agent and bulletin"); lib/api.ts's older agent types
 * are left alone and these are used here instead.
 */

import { apiGet, PATHS, type SourceRef } from '@/lib/api';
import { useApi } from '@/lib/live';

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

const EXAMPLES_QUERY = { limit: 8 } as const;

/** Verified runs: refreshed every few minutes, never on focus (they change only when a run is stored). */
export const useExamples = () =>
  useApi<ExamplesResponse>(PATHS.agentExamples, EXAMPLES_QUERY, { refreshInterval: 5 * 60_000, revalidateOnFocus: false });

export const AGENT_STATUS_PATH = '/api/agent/status';
export const useAgentStatus = () => useApi<AgentStatusResponse>(AGENT_STATUS_PATH, undefined, { refreshInterval: 60_000 });

export const loadRun = (id: string, signal?: AbortSignal) => apiGet<StoredRunResponse>(PATHS.agentRun(id), undefined, { signal, timeoutMs: 15_000 });

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
