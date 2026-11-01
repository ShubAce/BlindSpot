/**
 * Typed client for the BlindSpot API (docs/architecture.md section 5).
 *
 * - One function per endpoint, in `api`, plus `apiUrl()` so SWR keys and RSC fallbacks share the
 *   exact same URL string.
 * - Types mirror the contract. Fields the contract marks as maybe-missing are optional, and unknown
 *   extra fields are tolerated (nothing is validated away). Where a module's final shape is not
 *   fixed yet (geo, geostationary, stubble share, official counts) a small `read*` normaliser accepts
 *   the plausible shapes and returns one, so pages do not care. docs/api-contract-notes.md, once
 *   written by the integration lead, is the source of truth; update these types to match it.
 * - Errors are `ApiError` with a `kind` and a one-sentence `userMessage` for the honest error state.
 * - Every request has a timeout (AbortSignal.timeout) and can be aborted by the caller.
 *
 * Nothing here holds data. Server components use lib/api-server.ts (short-lived cache, never throws);
 * client components use the SWR hooks in lib/live.ts.
 */

import { bboxParam, type BBox } from './aoi';

// ---------------------------------------------------------------------------------------------
// base URL

/** NEXT_PUBLIC_API_URL is inlined at build time, so set it before `next build`. */
export const API_BASE: string = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').replace(/\/+$/, '');

// ---------------------------------------------------------------------------------------------
// shared shapes

/** Where a number came from. Every JSON response lists these under `sources`. */
export type SourceRef = {
  key: string;
  label: string;
  url: string | null;
  fetched_at: string | null;
  fetch_id: number | null;
};

/** Every JSON response carries these (UTC ISO-8601 with Z). */
export type Envelope = { generated_at: string; sources: SourceRef[] };

/** ISO-8601 UTC string, e.g. "2026-10-30T08:09:00Z". Formatting to IST is lib/format.ts's job. */
export type IsoTime = string;
export type LonLat = [number, number];

// Minimal GeoJSON, structurally compatible with @types/geojson (which deck.gl uses), declared here
// because pnpm does not expose deck.gl's transitive @types/geojson to our sources.
export type Position = number[];
export type PointGeometry = { type: 'Point'; coordinates: Position };
export type MultiPointGeometry = { type: 'MultiPoint'; coordinates: Position[] };
export type LineStringGeometry = { type: 'LineString'; coordinates: Position[] };
export type MultiLineStringGeometry = { type: 'MultiLineString'; coordinates: Position[][] };
export type PolygonGeometry = { type: 'Polygon'; coordinates: Position[][] };
export type MultiPolygonGeometry = { type: 'MultiPolygon'; coordinates: Position[][][] };
export type Geometry =
  | PointGeometry
  | MultiPointGeometry
  | LineStringGeometry
  | MultiLineStringGeometry
  | PolygonGeometry
  | MultiPolygonGeometry;
export type Feature<G extends Geometry = Geometry, P = Record<string, unknown>> = {
  type: 'Feature';
  id?: string | number;
  geometry: G;
  properties: P;
};
export type FeatureCollection<G extends Geometry = Geometry, P = Record<string, unknown>> = {
  type: 'FeatureCollection';
  features: Feature<G, P>[];
};
export type AreaGeometry = PolygonGeometry | MultiPolygonGeometry;

// ---------------------------------------------------------------------------------------------
// endpoint types

export type HealthResponse = { ok: boolean; time: IsoTime; db: boolean; version: string; scheduler?: boolean };

export type SourceStatusValue = 'ok' | 'stale' | 'error' | 'never';
export type SourceStatus = {
  key: string;
  label: string;
  kind: string;
  status: SourceStatusValue;
  last_success_at: IsoTime | null;
  last_attempt_at: IsoTime | null;
  last_error: string | null;
  records_last: number | null;
  latency_ms_last: number | null;
  cadence_minutes: number | null;
  docs_url: string | null;
};
/** GET /api/sources keeps its own `sources` key: the status of every upstream, not SourceRefs. */
export type SourcesResponse = { generated_at: string; sources: SourceStatus[] };

export type FireProps = {
  id: number;
  satellite: string; // Suomi NPP, NOAA-20, NOAA-21, Aqua, Terra
  instrument: string; // VIIRS / MODIS
  acq_utc: IsoTime;
  confidence: string; // low / nominal / high
  frp_mw: number | null;
  daynight: string; // D / N
  state: string | null;
  district: string | null;
  scan?: number | null;
  track?: number | null;
  source?: string;
};
export type FiresQuery = { hours?: number; bbox?: BBox; min_confidence?: 'low' | 'nominal' | 'high' };
export type FiresResponse = Envelope & {
  hours: number;
  count: number;
  by_satellite: Record<string, number>;
  features: FeatureCollection<PointGeometry, FireProps>;
};

export type Region = 'all' | 'punjab' | 'haryana';
export type FiresDailyResponse = Envelope & { series: { date: string; count: number; frp_sum: number | null }[] };

export type IstSpan = { start: string; end: string }; // "HH:MM" IST
export type FiresHoursResponse = Envelope & {
  total: number;
  bins: { hour_ist: number; count: number }[];
  by_satellite: Record<string, number[]>;
  observed_windows_ist: IstSpan[];
  unobserved_span_ist: IstSpan | null;
};

/** CREAMS official counts (contract addition in docs/research/SYNTHESIS.md, build-time updates).
 *  Shape not final: read it with `readOfficialDays`. */
export type FiresOfficialResponse = Envelope & {
  since?: string;
  total?: number;
  by_satellite?: Record<string, number>;
  days?: { date: string; events: number; published?: boolean; final?: boolean }[];
  series?: { date: string; events?: number; count?: number }[];
  features?: FeatureCollection<PointGeometry, Record<string, unknown>>;
};

export type OrbitTle = {
  key: string; // snpp, noaa20, noaa21, aqua, terra, s2a, s2b, s2c
  name: string; // Suomi NPP, NOAA-20, ...
  norad_id: number;
  instrument: string; // VIIRS / MODIS / MSI
  swath_km?: number | null; // full swath width
  half_swath_km?: number | null; // if the API states the criterion directly, it wins
  line1: string;
  line2: string;
  epoch: IsoTime;
  fetched_at: IsoTime;
  object_name?: string;
};
export type OrbitsTleResponse = Envelope & { satellites: OrbitTle[] };

export type Pass = {
  satellite: string;
  instrument: string;
  start_utc: IsoTime;
  peak_utc: IsoTime;
  end_utc: IsoTime;
  peak_elevation_deg: number;
  min_ground_distance_km: number;
};
export type PassesQuery = { lat: number; lon: number; hours_back?: number; hours_ahead?: number };
export type PassesResponse = Envelope & {
  site: { lat: number; lon: number; name?: string };
  passes: Pass[];
  current: { watching: { satellite: string; until_utc: IsoTime }[]; next: { satellite: string; at_utc: IsoTime } | null };
};

export type ScarProps = {
  id: number;
  area_ha: number;
  burn_after: IsoTime | string; // date of the clear pass before the burn
  burn_before: IsoTime | string; // date of the clear pass after it
  district: string | null;
  subdistrict: string | null;
  village: string | null;
  centroid?: LonLat | { lon: number; lat: number } | null;
  dnbr: number | null;
  confidence: number | string | null;
  /** true: "candidate unseen burn", no FIRMS detection explains it; false: an alert explains it;
   *  null: not yet checked against the fire alerts (the matching job has not run on this scar). */
  unseen: boolean | null;
  fire_matches: number | null;
  scene_pre: string | null;
  scene_post: string | null;
};
export type ScarsQuery = {
  since?: string;
  until?: string;
  district?: string;
  unseen?: boolean;
  bbox?: BBox;
  limit?: number;
};
export type ScarsResponse = Envelope & {
  totals: { clusters: number; area_ha: number; unseen_clusters: number; unseen_area_ha: number };
  features: FeatureCollection<AreaGeometry, ScarProps>;
};
export type ScarsSummaryResponse = Envelope & {
  season: {
    since: string;
    clusters: number;
    area_ha: number;
    unseen_area_ha: number;
    unseen_share: number;
    fire_detections: number;
  };
  by_district: {
    district: string;
    state: string;
    area_ha: number;
    clusters: number;
    unseen_area_ha: number;
    unseen_share: number;
    fire_detections: number;
  }[];
};
export type ImageryRef = {
  date: string;
  item_id: string;
  collection: string;
  truecolor_url?: string | null;
  swir_url?: string | null;
  nbr_url?: string | null;
};
export type ScarCaseResponse = Envelope & {
  case: ScarProps & { geometry?: AreaGeometry };
  imagery: { pre: ImageryRef | null; post: ImageryRef | null };
  fires_nearby: (FireProps & { distance_km?: number })[];
  weather_at_burn: Record<string, unknown> | null;
  passes_missed?: Pass[];
};

export type RiskProps = {
  id: number;
  area_ha: number;
  harvested_after: string;
  harvested_before: string;
  days_since_harvest: number | null;
  risk_score: number; // a relative index 0..100 (risk-v3: 2 decimals), not a probability
  risk_rank?: number | null; // 1 = highest score of the run
  risk_rank_of?: number | null;
  risk_percentile?: number | null;
  risk_band?: string | null; // 'top 1%', '1-5%', ...
  burn_probability?: number | null; // for the tested ordering only; see method.fields
  burn_probability_ci95?: [number, number] | null;
  burn_probability_band?: string | null;
  risk_factors: Record<string, unknown> | null;
  district: string | null;
  village: string | null;
  centroid?: LonLat | { lon: number; lat: number } | null;
};
export type RiskQuery = { district?: string; limit?: number };
export type RiskResponse = Envelope & {
  as_of: IsoTime;
  totals: { clusters: number; area_ha: number };
  features: FeatureCollection<AreaGeometry, RiskProps>;
  ranking: { village: string; district: string; area_ha: number; clusters: number; risk_score: number }[];
};

export type AirStation = {
  id: string | number;
  name: string;
  city: string | null;
  state: string | null;
  lat: number;
  lon: number;
  pm25: number | null;
  pm10: number | null;
  observed_at: IsoTime | null;
  provider: string; // openaq / datagov_aqi / cams
  is_model: boolean;
  /** Minutes since the reading (the API computes it); null when unknown. */
  age_minutes?: number | null;
  /** "µg/m³" or "naqi_subindex": data.gov.in publishes CPCB sub-indices, not concentrations. */
  unit?: string | null;
  pm25_unit?: string | null;
};
export type AirReceptor = {
  city: string;
  pm25_now: number | null;
  pm25_24h_mean: number | null;
  observed_at: IsoTime | null;
  provider: string;
  n_stations: number;
  unit?: string | null;
};
export type AirLatestResponse = Envelope & { stations: AirStation[]; receptors: AirReceptor[] };
export type AirSeriesResponse = Envelope & {
  city: string;
  series: { t: IsoTime; pm25: number | null; n_stations: number }[];
  model_series: { t: IsoTime; pm25: number | null }[];
};
/** IITM DSS stubble share (shape not final; read with `readStubbleShare`). */
export type StubbleShareResponse = Envelope & {
  run_date?: string;
  fetched_at?: IsoTime;
  daily_pct?: Record<string, number>;
  forecast_pct?: Record<string, unknown>;
  series?: { date: string; pct?: number; share?: number }[];
  url?: string;
};

export type Wind = { speed_kmh: number | null; from_deg: number | null };
export type MetNowResponse = Envelope & {
  city: string;
  t: IsoTime;
  wind_10m: Wind;
  wind_925: Wind;
  blh_m: number | null;
  temp_2m_c: number | null;
  inversion: boolean | null;
  rain_mm_24h: number | null;
};
export type WindGrid = { lon0: number; lat0: number; dlon: number; dlat: number; nx: number; ny: number };
export type WindFieldResponse = Envelope & {
  t: IsoTime;
  level: string;
  grid: WindGrid;
  /** m/s, row-major (index = j * nx + i, lat = lat0 + j * dlat), flat or as ny rows. */
  u: number[] | number[][];
  v: number[] | number[][];
  units?: string;
  model?: string;
};

export type SmokeTrail = {
  id: string | number;
  path: LonLat[];
  times: number[]; // unix seconds
  weight: number;
  origin: { type: string; id: string | number | null; district: string | null };
  /** best_match / gfs_seamless: the two-model ensemble (SYNTHESIS.md build-time updates). */
  model?: string;
};
export type SmokeReceptor = {
  city: string;
  series: { t: IsoTime; smoke_index: number }[];
  peak: { t: IsoTime; value: number } | null;
  arrival_t: IsoTime | null;
};
export type SmokeForwardResponse = Envelope & {
  run_at: IsoTime;
  t0: IsoTime;
  hours: number;
  step_minutes: number;
  inputs: { fires: number; scars: number };
  trails: SmokeTrail[];
  receptors: SmokeReceptor[];
  models?: string[];
  /** Optional uncertainty cone (spread between the ensemble members). */
  cone?: FeatureCollection<AreaGeometry, Record<string, unknown>> | null;
};
export type SmokeBackResponse = Envelope & {
  run_at: IsoTime;
  city: string;
  t0: IsoTime;
  trails: SmokeTrail[];
  summary: {
    share_over_burn_belt: { h24: number; h48: number; h72: number };
    fire_exposure_index: number | null;
    top_districts: { district: string; share: number }[];
    blh_m_now: number | null;
    wind_from_deg_now: number | null;
  };
  verdict_hint: 'stubble_likely' | 'mixed' | 'stubble_unlikely';
  models?: string[];
  cone?: FeatureCollection<AreaGeometry, Record<string, unknown>> | null;
};

export type ImageryKind = 'truecolor' | 'swir' | 'nbr';
export type FeaturedFrame = {
  date: string;
  item_id: string;
  collection: string;
  cloud_pct: number | null;
  truecolor_url: string;
  swir_url: string;
  width: number;
  height: number;
};
export type ImageryFeaturedResponse = Envelope & { aoi: { name: string; bbox: BBox }; frames: FeaturedFrame[] };

export type HeadlineResponse = Envelope & {
  fires_24h: { count: number; by_satellite: Record<string, number>; window_ist?: IstSpan | IstSpan[] | null };
  season: Record<string, unknown>;
  delhi: { pm25: number | null; observed_at: IsoTime | null; provider: string | null; unit?: string | null };
  passes: { last: { satellite: string; at_utc: IsoTime } | null; next: { satellite: string; at_utc: IsoTime } | null };
};

export type Citation = { marker: string; source: SourceRef };
export type BulletinLang = 'en' | 'hi' | 'pa';
export type BulletinResponse = Envelope & {
  date: string;
  lang: BulletinLang;
  generated_at: IsoTime;
  model: string;
  title: string;
  summary: string;
  sections: { heading: string; body: string }[];
  citations: Citation[];
};
export type AgentRunResponse = Envelope & {
  id: string | number;
  kind: 'ask' | 'bulletin';
  question: string | null;
  lang: string | null;
  model: string | null;
  started_at: IsoTime;
  finished_at: IsoTime | null;
  events: unknown[];
  answer: string | null;
  citations: Citation[] | null;
  verification: unknown;
  ok: boolean;
};
export type AgentExamplesResponse = Envelope & {
  runs?: Pick<AgentRunResponse, 'id' | 'question' | 'answer' | 'started_at' | 'model'>[];
  examples?: { id?: string | number; question: string; answer?: string | null; run_id?: string | number }[];
};

/** GET /api/agent/stats: questions (kinds ask and featured) and bulletins share this shape. `of` is
 *  the answered runs that record a field and `unknown` those stored before it existed: unknown runs
 *  are left out of the shares, never counted as false (contract notes, "Agent and bulletin"). Read
 *  by the method page and the investigator console's guardrail line. */
export type AgentGroupStats = {
  kinds: Record<string, number>;
  runs: number;
  answered: number;
  failed: number;
  first_run_at: IsoTime | null;
  last_run_at: IsoTime | null;
  figures: { checked: number; matched: number; matched_share: number | null; runs: number; unknown: number };
  all_matched: { runs: number; of: number; share: number | null; without_figures: number; unknown: number };
  corrected_figures: { runs: number; of: number; share: number | null; then_passed: number; then_passed_share: number | null; sentences_removed: number | null; unknown: number; how?: string };
  honesty: { passed: number; of: number; share: number | null; first_draft_issues: number; first_draft_issues_share: number | null; then_passed: number; then_passed_share: number | null; fallback_runs: number; fallback_sentences: number; unknown: number };
  duration_ms: { p50: number | null; p90: number | null; n: number };
  tool_calls: { median: number | null; n: number };
  models: { model: string; runs: number; share: number | null }[];
  languages: { lang: string; runs: number; share: number | null }[];
};
export type AgentStats = Envelope & {
  days: number;
  window: { from_utc: IsoTime; to_utc: IsoTime; first_day_ist: string; last_day_ist: string };
  basis: string | null;
  questions: AgentGroupStats | null;
  bulletins: AgentGroupStats | null;
};
/** The window both readers of /api/agent/stats ask for, so "the last 30 days" means the same on each. */
export const AGENT_STATS_DAYS = 30;

export type BoundaryLevel = 'state' | 'district' | 'subdistrict';
export type BoundaryProps = { level?: BoundaryLevel | string; name?: string | null; state?: string | null; code?: string | number | null };
/** GET /api/geo/boundaries: GeoJSON today; an MVT tile template is accepted too (`readBoundaries`). */
export type GeoBoundariesResponse = Partial<Envelope> & {
  type?: 'FeatureCollection';
  features?: Feature<Geometry, BoundaryProps>[] | FeatureCollection<Geometry, BoundaryProps>;
  boundaries?: FeatureCollection<Geometry, BoundaryProps>;
  tiles?: string | string[];
  layer?: string;
  attribution?: string;
};
export type City = { name: string; lat: number; lon: number; population?: number | null; state?: string | null; kind?: string };
export type GeoCitiesResponse = Partial<Envelope> & {
  cities?: City[];
  type?: 'FeatureCollection';
  features?: Feature<PointGeometry, Partial<City>>[];
};

export type GeoDiurnalBin = { hour_ist: number; pixels: number; scans?: number; clear_frac?: number | null };
export type GeostationaryDiurnalResponse = Partial<Envelope> & {
  date?: string;
  sensor?: string;
  state?: string;
  bins?: GeoDiurnalBin[];
  hours?: GeoDiurnalBin[];
  series?: GeoDiurnalBin[];
};
export type GeoPixelProps = { scan_utc?: IsoTime; btd_excess?: number | null; bt38?: number | null; bt11?: number | null; confidence?: string | number | null };
export type GeostationaryLatestResponse = Partial<Envelope> & {
  scan_utc?: IsoTime;
  sensor?: string;
  count?: number;
  pixels?: FeatureCollection<PointGeometry | AreaGeometry, GeoPixelProps> | Feature<PointGeometry | AreaGeometry, GeoPixelProps>[];
  type?: 'FeatureCollection';
  features?: Feature<PointGeometry | AreaGeometry, GeoPixelProps>[];
};

// ---------------------------------------------------------------------------------------------
// errors

export type ApiErrorKind = 'network' | 'timeout' | 'aborted' | 'http' | 'parse';

/** The backend's error body: {detail, status, path, generated_at, sources, ...}. */
export type ApiErrorBody = { detail?: unknown; status?: number; path?: string; source?: string; error?: string };

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly path: string;
  readonly body: ApiErrorBody | null;
  readonly retryAfterS: number | null;

  constructor(kind: ApiErrorKind, path: string, opts: { status?: number | null; body?: ApiErrorBody | null; retryAfterS?: number | null; message?: string } = {}) {
    super(opts.message ?? `${kind}${opts.status ? ` ${opts.status}` : ''} on ${path}`);
    this.name = 'ApiError';
    this.kind = kind;
    this.path = path;
    this.status = opts.status ?? null;
    this.body = opts.body ?? null;
    this.retryAfterS = opts.retryAfterS ?? null;
  }

  /** True when trying again soon may help (network, timeout, 429, 5xx). */
  get retryable(): boolean {
    if (this.kind === 'network' || this.kind === 'timeout') return true;
    return this.kind === 'http' && this.status != null && (this.status === 429 || this.status >= 500);
  }

  /** One plain sentence for the error state, without the word "error" or a stack. */
  get userMessage(): string {
    const detail = typeof this.body?.detail === 'string' ? this.body.detail : null;
    switch (this.kind) {
      case 'network':
        return 'The BlindSpot API is not reachable.';
      case 'timeout':
        return 'The BlindSpot API took too long to answer.';
      case 'aborted':
        return 'The request was cancelled.';
      case 'parse':
        return 'The API sent a reply this page could not read.';
      default:
        if (this.status === 404) return 'The API does not serve this yet.';
        if (this.status === 429) return 'The API asked us to slow down.';
        if (this.status === 503 && detail === 'database unavailable') return 'The API is up, but its database is not answering.';
        if (this.status === 502 || this.status === 503) return detail ? `An upstream source is unavailable (${detail}).` : 'An upstream source is unavailable.';
        if (this.status != null && this.status >= 500) return 'The API failed while preparing this.';
        return detail ? `The API refused the request: ${detail}.` : `The API refused the request (${this.status}).`;
    }
  }
}

/** "Couldn't load fire detections. The BlindSpot API is not reachable." */
export function honestError(what: string, err: unknown): string {
  const reason = err instanceof ApiError ? err.userMessage : 'Something went wrong in this page.';
  return `Couldn't load ${what}. ${reason}`;
}

// ---------------------------------------------------------------------------------------------
// transport

type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[];
export type Query = Record<string, QueryValue>;

/** Absolute URL for an API path. Arrays repeat the key; null/undefined are left out; booleans
 *  are sent as true/false; a 4-number bbox tuple is sent as "minLon,minLat,maxLon,maxLat". */
export function apiUrl(path: string, query?: Query, base: string = API_BASE): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v == null) continue;
    if (k === 'bbox' && Array.isArray(v) && v.length === 4) qs.append(k, bboxParam(v as unknown as BBox));
    else if (Array.isArray(v)) for (const item of v) qs.append(k, String(item));
    else qs.append(k, String(v));
  }
  const s = qs.toString();
  return `${base}${path.startsWith('/') ? path : `/${path}`}${s ? `?${s}` : ''}`;
}

export type RequestOptions = {
  /** Default 15 s; heavy GeoJSON endpoints pass more. */
  timeoutMs?: number;
  /** Abort from the caller (unmount, newer request). */
  signal?: AbortSignal;
  /** Override the base URL (server-side internal URL). */
  base?: string;
  init?: Omit<RequestInit, 'signal'>;
};

export const DEFAULT_TIMEOUT_MS = 15_000;

/** AbortSignal.any where available (Chrome 116+, Safari 17.4+), else a manual fan-in. */
function anySignal(signals: AbortSignal[]): AbortSignal {
  if (typeof AbortSignal.any === 'function') return AbortSignal.any(signals);
  const controller = new AbortController();
  for (const s of signals) {
    if (s.aborted) {
      controller.abort(s.reason);
      break;
    }
    s.addEventListener('abort', () => controller.abort(s.reason), { once: true });
  }
  return controller.signal;
}

function timeoutSignal(ms: number): AbortSignal {
  if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
  const controller = new AbortController();
  setTimeout(() => controller.abort(new DOMException('Timed out', 'TimeoutError')), ms);
  return controller.signal;
}

/** GET a URL and parse JSON, turning every failure into an ApiError. */
export async function getJSON<T>(url: string, opts: RequestOptions = {}): Promise<T> {
  const path = url.replace(/^https?:\/\/[^/]+/, '');
  const timeout = timeoutSignal(opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const signal = opts.signal ? anySignal([opts.signal, timeout]) : timeout;
  let res: Response;
  try {
    res = await fetch(url, { ...opts.init, headers: { Accept: 'application/json', ...opts.init?.headers }, signal });
  } catch (e) {
    if (opts.signal?.aborted) throw new ApiError('aborted', path);
    if (timeout.aborted) throw new ApiError('timeout', path);
    throw new ApiError('network', path, { message: e instanceof Error ? e.message : String(e) });
  }
  if (!res.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = (await res.json()) as ApiErrorBody;
    } catch {
      body = null; // not JSON (a proxy's HTML page); the status says enough
    }
    const ra = Number(res.headers.get('Retry-After'));
    throw new ApiError('http', path, { status: res.status, body, retryAfterS: Number.isFinite(ra) && ra > 0 ? ra : null });
  }
  try {
    return (await res.json()) as T;
  } catch (e) {
    if (opts.signal?.aborted) throw new ApiError('aborted', path);
    if (timeout.aborted) throw new ApiError('timeout', path);
    throw new ApiError('parse', path, { status: res.status, message: e instanceof Error ? e.message : String(e) });
  }
}

/** GET an API path with a query. */
export function apiGet<T>(path: string, query?: Query, opts: RequestOptions = {}): Promise<T> {
  return getJSON<T>(apiUrl(path, query, opts.base), opts);
}

// ---------------------------------------------------------------------------------------------
// endpoints: the paths in one place (SWR keys, server fetches and the client all use these)

export const PATHS = {
  health: '/api/health',
  sources: '/api/sources',
  fires: '/api/fires',
  firesDaily: '/api/fires/daily',
  firesHours: '/api/fires/hours',
  firesOfficial: '/api/fires/official',
  orbitsTle: '/api/orbits/tle',
  orbitsPasses: '/api/orbits/passes',
  scars: '/api/scars',
  scarsSummary: '/api/scars/summary',
  scarCase: (id: string | number) => `/api/scars/${encodeURIComponent(String(id))}`,
  risk: '/api/risk',
  airLatest: '/api/air/latest',
  airSeries: '/api/air/series',
  airStubbleShare: '/api/air/stubble-share',
  metNow: '/api/met/now',
  windField: '/api/met/wind-field',
  smokeForward: '/api/smoke/forward',
  smokeBack: '/api/smoke/back',
  imageryFeatured: '/api/imagery/featured',
  imageryRender: '/api/imagery/render',
  headline: '/api/stats/headline',
  agentAsk: '/api/agent/ask',
  agentRun: (id: string | number) => `/api/agent/runs/${encodeURIComponent(String(id))}`,
  agentExamples: '/api/agent/examples',
  agentStats: '/api/agent/stats',
  bulletinLatest: '/api/bulletin/latest',
  geoBoundaries: '/api/geo/boundaries',
  geoCities: '/api/geo/cities',
  geostationaryDiurnal: '/api/geostationary/diurnal',
  geostationaryLatest: '/api/geostationary/latest',
} as const;

/** Heavier responses (GeoJSON, grids, trails) get longer to arrive. */
const SLOW = { timeoutMs: 30_000 };

export const api = {
  health: (o?: RequestOptions) => apiGet<HealthResponse>(PATHS.health, undefined, o),
  sources: (o?: RequestOptions) => apiGet<SourcesResponse>(PATHS.sources, undefined, o),
  fires: (q: FiresQuery = {}, o?: RequestOptions) => apiGet<FiresResponse>(PATHS.fires, q, { ...SLOW, ...o }),
  firesDaily: (q: { since?: string; region?: Region } = {}, o?: RequestOptions) => apiGet<FiresDailyResponse>(PATHS.firesDaily, q, o),
  firesHours: (q: { days?: number; region?: Region } = {}, o?: RequestOptions) => apiGet<FiresHoursResponse>(PATHS.firesHours, q, o),
  firesOfficial: (q: { since?: string } = {}, o?: RequestOptions) => apiGet<FiresOfficialResponse>(PATHS.firesOfficial, q, o),
  orbitsTle: (o?: RequestOptions) => apiGet<OrbitsTleResponse>(PATHS.orbitsTle, undefined, o),
  orbitsPasses: (q: PassesQuery, o?: RequestOptions) => apiGet<PassesResponse>(PATHS.orbitsPasses, q, o),
  scars: (q: ScarsQuery = {}, o?: RequestOptions) => apiGet<ScarsResponse>(PATHS.scars, q, { ...SLOW, ...o }),
  scarsSummary: (q: { since?: string } = {}, o?: RequestOptions) => apiGet<ScarsSummaryResponse>(PATHS.scarsSummary, q, o),
  scarCase: (id: string | number, o?: RequestOptions) => apiGet<ScarCaseResponse>(PATHS.scarCase(id), undefined, o),
  risk: (q: RiskQuery = {}, o?: RequestOptions) => apiGet<RiskResponse>(PATHS.risk, q, { ...SLOW, ...o }),
  airLatest: (o?: RequestOptions) => apiGet<AirLatestResponse>(PATHS.airLatest, undefined, o),
  airSeries: (q: { city: string; hours?: number }, o?: RequestOptions) => apiGet<AirSeriesResponse>(PATHS.airSeries, q, o),
  airStubbleShare: (q: { since?: string } = {}, o?: RequestOptions) => apiGet<StubbleShareResponse>(PATHS.airStubbleShare, q, o),
  metNow: (q: { city: string }, o?: RequestOptions) => apiGet<MetNowResponse>(PATHS.metNow, q, o),
  windField: (q: { t?: string; level?: string } = {}, o?: RequestOptions) => apiGet<WindFieldResponse>(PATHS.windField, q, { ...SLOW, ...o }),
  smokeForward: (o?: RequestOptions) => apiGet<SmokeForwardResponse>(PATHS.smokeForward, undefined, { ...SLOW, ...o }),
  smokeBack: (q: { city: string; hours?: number }, o?: RequestOptions) => apiGet<SmokeBackResponse>(PATHS.smokeBack, q, { ...SLOW, ...o }),
  imageryFeatured: (o?: RequestOptions) => apiGet<ImageryFeaturedResponse>(PATHS.imageryFeatured, undefined, o),
  headline: (o?: RequestOptions) => apiGet<HeadlineResponse>(PATHS.headline, undefined, o),
  bulletinLatest: (q: { lang?: BulletinLang } = {}, o?: RequestOptions) => apiGet<BulletinResponse>(PATHS.bulletinLatest, q, o),
  agentRun: (id: string | number, o?: RequestOptions) => apiGet<AgentRunResponse>(PATHS.agentRun(id), undefined, o),
  agentExamples: (o?: RequestOptions) => apiGet<AgentExamplesResponse>(PATHS.agentExamples, undefined, o),
  geoBoundaries: (q: { level?: BoundaryLevel; bbox?: BBox } = {}, o?: RequestOptions) => apiGet<GeoBoundariesResponse>(PATHS.geoBoundaries, q, { ...SLOW, ...o }),
  geoCities: (q: { bbox?: BBox; min_population?: number } = {}, o?: RequestOptions) => apiGet<GeoCitiesResponse>(PATHS.geoCities, q, o),
  geostationaryDiurnal: (q: { date?: string; state?: string } = {}, o?: RequestOptions) => apiGet<GeostationaryDiurnalResponse>(PATHS.geostationaryDiurnal, q, o),
  geostationaryLatest: (o?: RequestOptions) => apiGet<GeostationaryLatestResponse>(PATHS.geostationaryLatest, undefined, { ...SLOW, ...o }),
};

// ---------------------------------------------------------------------------------------------
// URLs that are not JSON

/** XYZ template for Sentinel-2 tiles proxied by the API (deck.gl TileLayer / MapLibre raster). */
export function imageryTileTemplate(kind: ImageryKind, item: string, base: string = API_BASE): string {
  return `${base}/api/imagery/tiles/${kind}/${encodeURIComponent(item)}/{z}/{x}/{y}`;
}

/** One rendered image of a bbox (an evidence chip). Cached forever by the API. */
export function imageryRenderUrl(q: { collection: string; item: string; bbox: BBox; kind?: ImageryKind; w?: number; h?: number }): string {
  return apiUrl(PATHS.imageryRender, { ...q, bbox: q.bbox });
}

/** Resolve an image URL from the API (`truecolor_url` may be relative to the API). */
export function absoluteApiUrl(u: string | null | undefined, base: string = API_BASE): string | null {
  if (!u) return null;
  return /^https?:\/\//.test(u) ? u : `${base}${u.startsWith('/') ? '' : '/'}${u}`;
}

// ---------------------------------------------------------------------------------------------
// readers for shapes that are not final (each returns [] or null rather than guessing)

function isFeatureArray<G extends Geometry, P>(x: unknown): x is Feature<G, P>[] {
  return Array.isArray(x) && x.every((f) => f != null && typeof f === 'object' && (f as { type?: unknown }).type === 'Feature');
}
function featuresOf<G extends Geometry, P>(x: unknown): Feature<G, P>[] {
  if (x == null || typeof x !== 'object') return [];
  if (isFeatureArray<G, P>(x)) return x;
  const o = x as { type?: unknown; features?: unknown };
  if (isFeatureArray<G, P>(o.features)) return o.features;
  if (o.features && typeof o.features === 'object') return featuresOf<G, P>(o.features);
  return [];
}

export type BoundarySourceSpec =
  | { kind: 'geojson'; data: FeatureCollection<Geometry, BoundaryProps> }
  | { kind: 'vector'; tiles: string[]; layer: string; attribution?: string };

/** /api/geo/boundaries as GeoJSON (FeatureCollection, `{features}` or `{boundaries}`) or an MVT
 *  template (`{tiles, layer}`). Null when the reply is neither. */
export function readBoundaries(r: GeoBoundariesResponse | null | undefined): BoundarySourceSpec | null {
  if (!r) return null;
  if (r.tiles) {
    const tiles = (Array.isArray(r.tiles) ? r.tiles : [r.tiles]).map((t) => absoluteApiUrl(t) ?? t);
    return { kind: 'vector', tiles, layer: r.layer ?? 'boundaries', attribution: r.attribution };
  }
  const features = featuresOf<Geometry, BoundaryProps>(r.boundaries ?? r);
  return features.length ? { kind: 'geojson', data: { type: 'FeatureCollection', features } } : null;
}

export function readCities(r: GeoCitiesResponse | null | undefined): City[] {
  if (!r) return [];
  if (Array.isArray(r.cities)) return r.cities.filter((c) => Number.isFinite(c.lat) && Number.isFinite(c.lon));
  return featuresOf<PointGeometry, Partial<City>>(r)
    .map((f) => ({ ...f.properties, name: String(f.properties?.name ?? ''), lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] }))
    .filter((c) => c.name && Number.isFinite(c.lat) && Number.isFinite(c.lon));
}

/** Hourly GK-2A fire pixels in IST hours, sorted. */
export function readGeoDiurnal(r: GeostationaryDiurnalResponse | null | undefined): GeoDiurnalBin[] {
  const bins = r?.bins ?? r?.hours ?? r?.series ?? [];
  return bins
    .filter((b) => Number.isFinite(b.hour_ist) && Number.isFinite(b.pixels))
    .slice()
    .sort((a, b) => a.hour_ist - b.hour_ist);
}

export function readGeoPixels(r: GeostationaryLatestResponse | null | undefined): Feature<PointGeometry | AreaGeometry, GeoPixelProps>[] {
  if (!r) return [];
  return featuresOf<PointGeometry | AreaGeometry, GeoPixelProps>(r.pixels ?? r);
}

/** Official (CREAMS) events per IST day, oldest first. */
export function readOfficialDays(r: FiresOfficialResponse | null | undefined): { date: string; events: number; final?: boolean }[] {
  if (!r) return [];
  if (Array.isArray(r.days)) return r.days.map((d) => ({ date: d.date, events: d.events, final: d.final })).sort((a, b) => a.date.localeCompare(b.date));
  if (Array.isArray(r.series)) return r.series.map((d) => ({ date: d.date, events: d.events ?? d.count ?? 0 })).sort((a, b) => a.date.localeCompare(b.date));
  return [];
}

/** IITM DSS stubble share as [{date, pct}], pct in 0-100, oldest first. */
export function readStubbleShare(r: StubbleShareResponse | null | undefined): { date: string; pct: number }[] {
  if (!r) return [];
  if (Array.isArray(r.series)) {
    return r.series
      .map((d) => ({ date: d.date, pct: d.pct ?? (d.share != null ? d.share * 100 : NaN) }))
      .filter((d) => Number.isFinite(d.pct))
      .sort((a, b) => a.date.localeCompare(b.date));
  }
  if (r.daily_pct) {
    return Object.entries(r.daily_pct)
      .map(([date, pct]) => ({ date, pct: Number(pct) }))
      .filter((d) => Number.isFinite(d.pct))
      .sort((a, b) => a.date.localeCompare(b.date));
  }
  return [];
}

/** A scar's or risk cluster's centroid as [lon, lat], from either representation. */
export function centroidOf(c: LonLat | { lon: number; lat: number } | null | undefined): LonLat | null {
  if (!c) return null;
  if (Array.isArray(c)) return Number.isFinite(c[0]) && Number.isFinite(c[1]) ? [c[0], c[1]] : null;
  return Number.isFinite(c.lon) && Number.isFinite(c.lat) ? [c.lon, c.lat] : null;
}

/** The newest upstream fetch behind a response, or its generation time. */
export function dataTime(r: Partial<Envelope> | null | undefined): { at: string; basis: 'fetched_at' | 'generated_at' } | null {
  if (!r) return null;
  const fetched = (r.sources ?? []).map((s) => s.fetched_at).filter((t): t is string => !!t);
  if (fetched.length) return { at: fetched.reduce((a, b) => (Date.parse(a) >= Date.parse(b) ? a : b)), basis: 'fetched_at' };
  return r.generated_at ? { at: r.generated_at, basis: 'generated_at' } : null;
}
