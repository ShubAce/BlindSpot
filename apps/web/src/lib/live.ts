/**
 * Live data for client components: one SWR hook per endpoint, polling at the cadence of the job
 * that refreshes it, plus `useNow`, a freshness helper and the investigator stream as a hook.
 *
 * Every hook keeps the previous data while it revalidates (`keepPreviousData`), so a refresh never
 * flashes a skeleton (dataviz anti-pattern) and a failed refresh leaves the last good data on screen
 * next to the error: render `data` if present, the error sentence if `error`, the skeleton only when
 * neither exists yet. Polling pauses while the tab is hidden (SWR's default).
 *
 *   const { data, error, isLoading, mutate } = useFires({ hours: 24 });
 *   const fresh = useFreshness(data, CADENCE.fires);  // "updated 4 min ago", stale after 3 cadences
 *
 * Keys are the absolute URLs from `apiUrl`, so a Server Component can hand first-paint data over:
 *   <SWRConfig value={{ fallback: { [swrKey(PATHS.headline)]: result.data } }}>
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import useSWR, { type SWRConfiguration, type SWRResponse } from 'swr';

import {
  ApiError,
  apiUrl,
  dataTime,
  getJSON,
  PATHS,
  type AirLatestResponse,
  type AirSeriesResponse,
  type AgentExamplesResponse,
  type BoundaryLevel,
  type BulletinLang,
  type BulletinResponse,
  type Citation,
  type Envelope,
  type FiresDailyResponse,
  type FiresHoursResponse,
  type FiresOfficialResponse,
  type FiresQuery,
  type FiresResponse,
  type GeoBoundariesResponse,
  type GeoCitiesResponse,
  type GeostationaryDiurnalResponse,
  type GeostationaryLatestResponse,
  type HeadlineResponse,
  type HealthResponse,
  type ImageryFeaturedResponse,
  type MetNowResponse,
  type OrbitsTleResponse,
  type PassesQuery,
  type PassesResponse,
  type Query,
  type Region,
  type RiskQuery,
  type RiskResponse,
  type ScarCaseResponse,
  type ScarsQuery,
  type ScarsResponse,
  type ScarsSummaryResponse,
  type SmokeBackResponse,
  type SmokeForwardResponse,
  type SourcesResponse,
  type StubbleShareResponse,
  type WindFieldResponse,
} from './api';
import { CADENCE } from './cadence';
import { relative, toDate } from './format';
import { streamAgent, type AgentEvent, type AskBody } from './sse';

export { CADENCE } from './cadence';

/** The SWR key (and RSC fallback key) for an endpoint. */
export const swrKey = (path: string, query?: Query): string => apiUrl(path, query);

const fetchers = new Map<number, (url: string) => Promise<unknown>>();
function fetcherFor(timeoutMs: number) {
  let f = fetchers.get(timeoutMs);
  if (!f) {
    f = (url: string) => getJSON<unknown>(url, { timeoutMs });
    fetchers.set(timeoutMs, f);
  }
  return f;
}

/** Retry transient failures with backoff; a 4xx (other than 429) will not fix itself. */
const onErrorRetry: SWRConfiguration['onErrorRetry'] = (err, _key, _config, revalidate, { retryCount }) => {
  if (err instanceof ApiError && !err.retryable) return;
  if (retryCount >= 6) return;
  const wait = err instanceof ApiError && err.retryAfterS ? err.retryAfterS * 1000 : Math.min(60_000, 2_000 * 2 ** retryCount);
  setTimeout(() => void revalidate({ retryCount }), wait);
};

export type LiveOptions<T> = SWRConfiguration<T, ApiError> & { timeoutMs?: number };
export type Live<T> = SWRResponse<T, ApiError>;

/** The generic hook: GET `path?query`, polling every `refreshInterval` ms. A null path pauses it. */
export function useApi<T>(path: string | null, query?: Query, opts: LiveOptions<T> = {}): Live<T> {
  const { timeoutMs = 15_000, ...config } = opts;
  const key = path ? swrKey(path, query) : null;
  return useSWR<T, ApiError>(key, fetcherFor(timeoutMs) as (url: string) => Promise<T>, {
    keepPreviousData: true,
    revalidateOnFocus: true,
    dedupingInterval: 5_000,
    onErrorRetry,
    ...config,
  });
}

const SLOW = 30_000;
const STATIC = { refreshInterval: 0, revalidateOnFocus: false, revalidateIfStale: false } as const;

export const useHealth = (o?: LiveOptions<HealthResponse>) => useApi<HealthResponse>(PATHS.health, undefined, { refreshInterval: CADENCE.health, ...o });
export const useSources = (o?: LiveOptions<SourcesResponse>) => useApi<SourcesResponse>(PATHS.sources, undefined, { refreshInterval: CADENCE.sources, ...o });
export const useHeadline = (o?: LiveOptions<HeadlineResponse>) => useApi<HeadlineResponse>(PATHS.headline, undefined, { refreshInterval: CADENCE.headline, ...o });

export const useFires = (q: FiresQuery = {}, o?: LiveOptions<FiresResponse>) => useApi<FiresResponse>(PATHS.fires, q, { refreshInterval: CADENCE.fires, timeoutMs: SLOW, ...o });
export const useFiresDaily = (q: { since?: string; region?: Region } = {}, o?: LiveOptions<FiresDailyResponse>) => useApi<FiresDailyResponse>(PATHS.firesDaily, q, { refreshInterval: CADENCE.scars, ...o });
export const useFiresHours = (q: { days?: number; region?: Region } = {}, o?: LiveOptions<FiresHoursResponse>) => useApi<FiresHoursResponse>(PATHS.firesHours, q, { refreshInterval: CADENCE.fires, ...o });
export const useFiresOfficial = (q: { since?: string } = {}, o?: LiveOptions<FiresOfficialResponse>) => useApi<FiresOfficialResponse>(PATHS.firesOfficial, q, { refreshInterval: CADENCE.official, ...o });

export const useOrbitsTle = (o?: LiveOptions<OrbitsTleResponse>) => useApi<OrbitsTleResponse>(PATHS.orbitsTle, undefined, { refreshInterval: CADENCE.orbits, ...o });
/** Pass null to wait for a site. */
export const usePasses = (q: PassesQuery | null, o?: LiveOptions<PassesResponse>) => useApi<PassesResponse>(q ? PATHS.orbitsPasses : null, q ?? undefined, { refreshInterval: CADENCE.passes, ...o });

export const useScars = (q: ScarsQuery = {}, o?: LiveOptions<ScarsResponse>) => useApi<ScarsResponse>(PATHS.scars, q, { refreshInterval: CADENCE.scars, timeoutMs: SLOW, ...o });
export const useScarsSummary = (q: { since?: string } = {}, o?: LiveOptions<ScarsSummaryResponse>) => useApi<ScarsSummaryResponse>(PATHS.scarsSummary, q, { refreshInterval: CADENCE.scars, ...o });
export const useScarCase = (id: string | number | null, o?: LiveOptions<ScarCaseResponse>) => useApi<ScarCaseResponse>(id == null ? null : PATHS.scarCase(id), undefined, { refreshInterval: CADENCE.scars, ...o });
export const useRisk = (q: RiskQuery = {}, o?: LiveOptions<RiskResponse>) => useApi<RiskResponse>(PATHS.risk, q, { refreshInterval: CADENCE.risk, timeoutMs: SLOW, ...o });

export const useAirLatest = (o?: LiveOptions<AirLatestResponse>) => useApi<AirLatestResponse>(PATHS.airLatest, undefined, { refreshInterval: CADENCE.air, ...o });
export const useAirSeries = (q: { city: string; hours?: number } | null, o?: LiveOptions<AirSeriesResponse>) => useApi<AirSeriesResponse>(q ? PATHS.airSeries : null, q ?? undefined, { refreshInterval: CADENCE.air, ...o });
export const useStubbleShare = (q: { since?: string } = {}, o?: LiveOptions<StubbleShareResponse>) => useApi<StubbleShareResponse>(PATHS.airStubbleShare, q, { refreshInterval: CADENCE.stubble, ...o });

export const useMetNow = (q: { city: string } | null, o?: LiveOptions<MetNowResponse>) => useApi<MetNowResponse>(q ? PATHS.metNow : null, q ?? undefined, { refreshInterval: CADENCE.met, ...o });
export const useWindField = (q: { t?: string; level?: string } = {}, o?: LiveOptions<WindFieldResponse>) => useApi<WindFieldResponse>(PATHS.windField, q, { refreshInterval: CADENCE.wind, timeoutMs: SLOW, ...o });
export const useSmokeForward = (o?: LiveOptions<SmokeForwardResponse>) => useApi<SmokeForwardResponse>(PATHS.smokeForward, undefined, { refreshInterval: CADENCE.smoke, timeoutMs: SLOW, ...o });
export const useSmokeBack = (q: { city: string; hours?: number } | null, o?: LiveOptions<SmokeBackResponse>) => useApi<SmokeBackResponse>(q ? PATHS.smokeBack : null, q ?? undefined, { refreshInterval: CADENCE.smoke, timeoutMs: SLOW, ...o });

export const useImageryFeatured = (o?: LiveOptions<ImageryFeaturedResponse>) => useApi<ImageryFeaturedResponse>(PATHS.imageryFeatured, undefined, { refreshInterval: CADENCE.imagery, ...o });
export const useBulletin = (lang: BulletinLang = 'en', o?: LiveOptions<BulletinResponse>) => useApi<BulletinResponse>(PATHS.bulletinLatest, { lang }, { refreshInterval: CADENCE.bulletin, ...o });
export const useAgentExamples = (o?: LiveOptions<AgentExamplesResponse>) => useApi<AgentExamplesResponse>(PATHS.agentExamples, undefined, { ...STATIC, ...o });

export const useBoundaries = (q: { level?: BoundaryLevel } = {}, o?: LiveOptions<GeoBoundariesResponse>) => useApi<GeoBoundariesResponse>(PATHS.geoBoundaries, q, { ...STATIC, timeoutMs: SLOW, ...o });
export const useCities = (q: { min_population?: number } = {}, o?: LiveOptions<GeoCitiesResponse>) => useApi<GeoCitiesResponse>(PATHS.geoCities, q, { ...STATIC, ...o });
export const useGeostationaryDiurnal = (q: { date?: string; state?: string } = {}, o?: LiveOptions<GeostationaryDiurnalResponse>) => useApi<GeostationaryDiurnalResponse>(PATHS.geostationaryDiurnal, q, { refreshInterval: CADENCE.geostationary, ...o });
export const useGeostationaryLatest = (o?: LiveOptions<GeostationaryLatestResponse>) => useApi<GeostationaryLatestResponse>(PATHS.geostationaryLatest, undefined, { refreshInterval: CADENCE.geostationary, timeoutMs: SLOW, ...o });

// ---------------------------------------------------------------------------------------------
// the clock: one shared interval per period, so fifty "4 min ago" labels re-render together

type Ticker = { now: number; listeners: Set<() => void>; timer: ReturnType<typeof setInterval> | null };
const tickers = new Map<number, Ticker>();

function ticker(period: number): Ticker {
  let t = tickers.get(period);
  if (!t) {
    t = { now: Date.now(), listeners: new Set(), timer: null };
    tickers.set(period, t);
  }
  return t;
}

function subscribeTicker(period: number, cb: () => void): () => void {
  const t = ticker(period);
  t.listeners.add(cb);
  if (!t.timer) {
    t.now = Date.now();
    t.timer = setInterval(() => {
      t.now = Date.now();
      for (const l of t.listeners) l();
    }, period);
  }
  return () => {
    t.listeners.delete(cb);
    if (!t.listeners.size && t.timer) {
      clearInterval(t.timer);
      t.timer = null;
    }
  };
}

/**
 * The current time, re-rendering every `periodMs`. Null during server rendering and hydration, so
 * time-dependent text ("in 9 h 41 min") is only ever computed in the browser: render a skeleton of
 * the right width while it is null.
 */
export function useNow(periodMs = 1_000): Date | null {
  const subscribe = useCallback((cb: () => void) => subscribeTicker(periodMs, cb), [periodMs]);
  const getSnapshot = useCallback(() => ticker(periodMs).now, [periodMs]);
  const t = useSyncExternalStore<number | null>(subscribe, getSnapshot, () => null);
  return useMemo(() => (t == null ? null : new Date(t)), [t]);
}

// ---------------------------------------------------------------------------------------------
// freshness

export type Freshness = {
  at: Date;
  ageMs: number;
  /** "updated 4 min ago" */
  label: string;
  /** Older than three cadences: the job has missed at least two runs (the backend's rule too). */
  stale: boolean;
  basis: 'fetched_at' | 'generated_at';
};

/** How old the data behind a response is: the newest upstream `fetched_at` among its sources,
 *  else its `generated_at`. */
export function freshness(resp: Partial<Envelope> | null | undefined, now: Date | null, cadenceMs?: number, verb = 'updated'): Freshness | null {
  const dt = dataTime(resp);
  const at = toDate(dt?.at);
  if (!dt || !at || !now) return null;
  const ageMs = Math.max(0, now.getTime() - at.getTime());
  return {
    at,
    ageMs,
    label: `${verb} ${relative(at, now)}`,
    stale: cadenceMs ? ageMs > 3 * cadenceMs : false,
    basis: dt.basis,
  };
}

export function useFreshness(resp: Partial<Envelope> | null | undefined, cadenceMs?: number, verb = 'updated'): Freshness | null {
  const now = useNow(15_000);
  return useMemo(() => freshness(resp, now, cadenceMs, verb), [resp, now, cadenceMs, verb]);
}

// ---------------------------------------------------------------------------------------------
// the investigator as a hook

export type AgentStatus = 'idle' | 'streaming' | 'done' | 'error' | 'stopped';
export type ToolStep = {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: { ok: boolean; summary: string; sources: Extract<AgentEvent, { type: 'tool_result' }>['sources'] };
};
export type AgentRunState = {
  status: AgentStatus;
  question: string | null;
  runId: string | null;
  model: string | null;
  thoughts: string[];
  steps: ToolStep[];
  /** The answer as it streams; replaced by the final text on `answer`. */
  answer: string;
  citations: Citation[];
  verification: Extract<AgentEvent, { type: 'verification' }> | null;
  error: { message: string; retryable: boolean } | null;
  durationMs: number | null;
  /** Every event in order, for a timeline view. */
  events: AgentEvent[];
};

export const initialAgentState: AgentRunState = {
  status: 'idle',
  question: null,
  runId: null,
  model: null,
  thoughts: [],
  steps: [],
  answer: '',
  citations: [],
  verification: null,
  error: null,
  durationMs: null,
  events: [],
};

/** Pure reducer from one event to the next state (exported for tests and replays of stored runs). */
export function reduceAgentEvent(s: AgentRunState, ev: AgentEvent): AgentRunState {
  const next: AgentRunState = { ...s, events: [...s.events, ev] };
  switch (ev.type) {
    case 'run':
      return { ...next, runId: ev.run_id, model: ev.model };
    case 'thought':
      return { ...next, thoughts: [...s.thoughts, ev.text] };
    case 'tool_call':
      return { ...next, steps: [...s.steps, { id: ev.id, name: ev.name, args: ev.args ?? {} }] };
    case 'tool_result':
      return {
        ...next,
        steps: s.steps.some((t) => t.id === ev.id)
          ? s.steps.map((t) => (t.id === ev.id ? { ...t, result: { ok: ev.ok, summary: ev.summary, sources: ev.sources ?? [] } } : t))
          : [...s.steps, { id: ev.id, name: ev.name, args: {}, result: { ok: ev.ok, summary: ev.summary, sources: ev.sources ?? [] } }],
      };
    case 'answer_delta':
      return { ...next, answer: s.answer + (ev.text ?? '') };
    case 'answer':
      return { ...next, answer: ev.text ?? s.answer, citations: ev.citations ?? [] };
    case 'verification':
      return { ...next, verification: ev };
    case 'error':
      return { ...next, status: 'error', error: { message: ev.message, retryable: ev.retryable } };
    case 'done':
      return { ...next, status: s.status === 'error' ? 'error' : 'done', durationMs: ev.duration_ms ?? null };
    default:
      return next;
  }
}

/**
 * Ask the investigator and watch it work.
 *   const run = useAgentRun({ onMap: (ev) => map.flyTo(...) });
 *   run.ask('Why was Delhi\'s air bad this morning?');  run.stop();
 */
export function useAgentRun(opts: { onMap?: (ev: Extract<AgentEvent, { type: 'map' }>) => void } = {}) {
  const [state, setState] = useState<AgentRunState>(initialAgentState);
  const ctrl = useRef<AbortController | null>(null);
  const onMap = useRef(opts.onMap);
  useEffect(() => {
    onMap.current = opts.onMap;
  }, [opts.onMap]);

  const ask = useCallback(async (question: string, lang?: AskBody['lang']) => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setState({ ...initialAgentState, status: 'streaming', question });
    for await (const ev of streamAgent({ question, lang }, { signal: c.signal })) {
      if (c.signal.aborted) return;
      if (ev.type === 'map') onMap.current?.(ev);
      setState((s) => reduceAgentEvent(s, ev));
    }
    if (!c.signal.aborted) setState((s) => (s.status === 'streaming' ? { ...s, status: 'done' } : s));
  }, []);

  const stop = useCallback(() => {
    ctrl.current?.abort();
    setState((s) => (s.status === 'streaming' ? { ...s, status: 'stopped' } : s));
  }, []);

  const reset = useCallback(() => {
    ctrl.current?.abort();
    setState(initialAgentState);
  }, []);

  useEffect(() => () => ctrl.current?.abort(), []);

  return { ...state, ask, stop, reset };
}
