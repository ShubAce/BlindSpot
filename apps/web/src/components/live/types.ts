/**
 * Response shapes mission control reads, as served live (docs/api-contract-notes.md, 2026-10-31).
 * The shared client (lib/api.ts) predates some of these final shapes; these types extend it here,
 * for /live only, without touching the shared file. Every block the API may withhold is optional.
 */

import type { AreaGeometry, Envelope, FeatureCollection, ScarProps, SmokeTrail } from '@/lib/api';

/** A headline block: `available: false` with a `reason` when its inputs are missing. */
export type Block<T> = { available?: boolean; reason?: string | null } & Partial<T>;

export type HeadlinePass = {
  satellite: string;
  instrument: string;
  at_utc: string;
  start_utc: string;
  end_utc: string;
  min_ground_distance_km: number;
};

export type SmokeTrace = {
  run_at: string;
  verdict_hint: 'stubble_likely' | 'mixed' | 'stubble_unlikely' | string;
  verdict_reason: string | null;
  share_over_burn_belt: { h24: number | null; h48: number | null; h72: number | null } | null;
  fire_exposure_index: number | null;
  fire_exposure_range: [number, number] | null;
};

export type LastReading = { value: number | null; observed_at: string | null; n_stations: number | null; age_minutes: number | null; stale: boolean };

/** GET /api/stats/headline */
export type LiveHeadline = Envelope & {
  as_of?: string;
  fires_24h?: Block<{
    count: number;
    by_satellite: Record<string, number>;
    by_state: Record<string, number>;
    window: { from_utc: string; to_utc: string };
    latest_acq_utc: string | null;
    note: string | null;
  }>;
  season?: Block<{
    since: string;
    until: string;
    firms_detections: number;
    firms_complete_days: number;
    firms_days: number;
    official_events: number;
    official_source: string;
    note: string | null;
  }>;
  unseen_burns?: Block<{
    label: string;
    clusters: number;
    area_ha: number;
    unseen_clusters: number;
    unseen_area_ha: number;
    checked_area_ha: number;
    unseen_share: number | null;
    districts: number;
    first_pass: string | null;
    last_pass: string | null;
    model_version: string;
    precision: number | null;
    precision_ci95: [number, number] | null;
    precision_n: number | null;
    note: string | null;
  }>;
  delhi?: Block<{
    city: string;
    pm25: number | null;
    unit: string | null;
    observed_at: string | null;
    provider: string | null;
    n_stations: number;
    stale: boolean;
    last: LastReading | null;
    pm25_24h_mean: number | null;
    aqi: number | null;
    basis: string | null;
    smoke_trace: SmokeTrace | null;
  }>;
  passes?: Block<{
    site: { lat: number; lon: number; name: string; basis?: string };
    last: HeadlinePass | null;
    next: HeadlinePass | null;
    watching: { satellite: string; instrument?: string; until_utc?: string }[];
    blind_for_minutes: number | null;
    instruments: string[];
    note: string | null;
  }>;
  gk2a_evening?: Block<{
    unit: string;
    days: number;
    peak_ist: string | null;
    pixel_hours: number | null;
    share_outside_polar_windows: number | null;
    unobserved_span_ist: { start: string; end: string; minutes: number; pixel_hours: number | null } | null;
    note: string | null;
  }>;
};

/** A scar's properties as /api/scars serves them now (the shared type plus the newer fields). */
export type LiveScarProps = ScarProps & {
  label?: string;
  state?: string | null;
  confidence: number | null;
  nearest_fire_m?: number | null;
  gap_days?: number | null;
  nbr_post?: number | null;
  village_km?: number | null;
  hazy?: boolean | null;
  res_m?: number | null;
};

/** GET /api/scars */
export type LiveScarsResponse = Envelope & {
  totals: { clusters: number; area_ha: number; unseen_clusters: number; unseen_area_ha: number; checked_clusters?: number };
  returned?: number;
  truncated?: boolean;
  window?: { first_pass: string | null; last_pass: string | null };
  features: FeatureCollection<AreaGeometry, LiveScarProps>;
};

/** GET /api/scars/summary (by_district only, the part /live reads). */
export type ScarsSummaryLive = Envelope & {
  season?: { since: string; unseen_area_ha: number; area_ha?: number | null; clusters?: number | null; last_pass?: string | null };
  by_district?: { district: string; state: string; area_ha: number; clusters: number; unseen_area_ha: number; unseen_share: number | null; fire_detections?: number | null }[];
};

/** GET /api/scars/{id} (the case summary reads these). */
export type ImagerySide = {
  date: string;
  datetime?: string;
  item_id: string;
  collection: string;
  cloud_pct?: number | null;
  truecolor_url?: string | null;
  swir_url?: string | null;
};
export type LiveScarCase = Envelope & {
  case: LiveScarProps & { geometry?: AreaGeometry };
  imagery: { bbox?: [number, number, number, number]; pre: ImagerySide | null; post: ImagerySide | null } | null;
  fires_nearby: { id: number; satellite: string; acq_utc: string; distance_m: number; in_window: boolean; explains_scar: boolean }[];
};

/** GET /api/fires/daily */
export type FiresDailyLive = Envelope & {
  since: string;
  until: string;
  series: { date: string; count: number; frp_sum: number | null; complete: boolean }[];
  totals?: { count: number };
  note?: string | null;
};

/** GET /api/geostationary/diurnal */
export type DiurnalLive = Envelope & {
  days: number;
  unit: string;
  scans: number;
  hourly_ist: { hour: number; mean_count: number | null; scans: number }[];
  polar_windows_ist: { start: string; end: string; satellites: string[] }[];
  peak_ist: string | null;
  peak_mean_count: number | null;
  unobserved_gaps_ist: { start: string; end: string; minutes: number; pixel_hours: number | null }[];
  interpretation: string[];
};

/** GET /api/air/latest (the receptors, the part the air panel reads). */
export type AirReceptorLive = {
  city: string;
  state: string | null;
  pm25_now: number | null;
  pm25_24h_mean: number | null;
  observed_at: string | null;
  provider: string | null;
  n_stations: number;
  n_stations_total?: number;
  pm25_last: LastReading | null;
  lat: number | null;
  lon: number | null;
  model: { pm25: number | null; pm10: number | null; valid_at: string | null; provider: string } | null;
};
export type AirLatestLive = Envelope & {
  as_of?: string;
  fresh_minutes?: number;
  receptors: AirReceptorLive[];
  counts?: { stations: number; fresh: number; stale: number };
  reason?: string | null;
};

/** GET /api/smoke/receptors */
export type SmokeReceptorLive = {
  city: string;
  lat: number;
  lon: number;
  state: string | null;
  run_at: string | null;
  verdict_hint: string | null;
  verdict_reason: string | null;
  share_over_burn_belt: { h24: number | null; h48: number | null; h72: number | null } | null;
  fire_exposure_index: number | null;
  fire_exposure_range: [number, number] | null;
};
export type SmokeReceptorsLive = Envelope & { receptors: SmokeReceptorLive[] };

/** GET /api/smoke/back */
export type SmokeBackLive = Envelope & {
  run_at: string;
  city: string;
  t0: string;
  /** Span of the back run in hours (72 on 2026-10-31). */
  hours?: number | null;
  trails: SmokeTrail[];
  verdict_hint: string | null;
  summary: {
    cone?: { type: 'Polygon' | 'MultiPolygon'; coordinates: number[][][] | number[][][][] } | null;
    members?: { model: string; centerline?: { path: [number, number][] } | null }[];
    fire_exposure_index?: number | null;
    fire_exposure_range?: [number, number] | null;
    verdict_reason?: string | null;
  };
};
