/**
 * The response fields the landing page reads, typed from docs/api-contract-notes.md (2026-10-31).
 *
 * lib/api.ts keeps the shared types; where its older shapes are thinner than the final contract (the
 * headline, the scar list, the risk ranking, the agent examples) the landing reads these instead,
 * against the same endpoints and the same SWR keys. Every field the contract marks as maybe-missing
 * is optional or nullable here, so a reply missing it renders an honest line, never a guess.
 */

import type { Envelope, FeatureCollection, AreaGeometry, IsoTime, LonLat, SourceRef } from '@/lib/api';

// ---------------------------------------------------------------------------------------------
// GET /api/stats/headline

type Block = { available: boolean; reason?: string | null; note?: string | null };

export type PassRef = {
  satellite: string;
  instrument: string;
  at_utc: IsoTime;
  start_utc?: IsoTime;
  end_utc?: IsoTime;
  min_ground_distance_km?: number;
};

export type Headline = Envelope & {
  as_of?: IsoTime;
  fires_24h?: Block & {
    count?: number;
    by_satellite?: Record<string, number>;
    by_state?: Record<string, number>;
    window_ist?: { from: string; to: string };
    latest_acq_utc?: IsoTime | null;
    region?: string;
  };
  season?: Block & {
    since?: string;
    firms_detections?: number;
    official_events?: number;
    official_source?: string;
  };
  unseen_burns?: Block & {
    label?: string;
    clusters?: number;
    area_ha?: number;
    unseen_clusters?: number;
    unseen_area_ha?: number;
    checked_area_ha?: number;
    unseen_share?: number | null;
    districts?: number;
    first_pass?: IsoTime | null;
    last_pass?: IsoTime | null;
    precision?: number | null;
    precision_ci95?: [number, number] | null;
    precision_n?: number | null;
  };
  delhi?: Block & {
    city?: string;
    pm25?: number | null;
    unit?: string;
    observed_at?: IsoTime | null;
    provider?: string;
    n_stations?: number;
    stale?: boolean;
    last?: { value: number | null; observed_at: IsoTime | null; n_stations?: number; age_minutes?: number | null; stale?: boolean } | null;
    pm25_24h_mean?: number | null;
    basis?: string;
    smoke_trace?: {
      run_at?: IsoTime;
      verdict_hint?: 'stubble_likely' | 'mixed' | 'stubble_unlikely' | string;
      verdict_reason?: string;
      share_over_burn_belt?: { h24?: number; h48?: number; h72?: number };
      fire_exposure_index?: number | null;
      fire_exposure_range?: [number, number] | null;
    } | null;
  };
  passes?: Block & {
    site?: { lat: number; lon: number; name?: string };
    last?: PassRef | null;
    next?: PassRef | null;
    watching?: PassRef[];
    blind_for_minutes?: number | null;
  };
  gk2a_evening?: Block & {
    unit?: string;
    days?: number;
    peak_ist?: string | null;
    share_outside_polar_windows?: number | null;
    pixel_hours?: number | null;
    unobserved_span_ist?: { start: string; end: string; minutes?: number; pixel_hours?: number | null } | null;
  };
};

// ---------------------------------------------------------------------------------------------
// GET /api/scars (unseen=true) and GET /api/scars/{id}

export type ScarListProps = {
  id: number;
  area_ha: number;
  burn_after: IsoTime;
  burn_before: IsoTime;
  district: string | null;
  subdistrict?: string | null;
  village: string | null;
  village_km?: number | null;
  confidence: number | null;
  unseen: boolean | null;
  label?: string;
  fire_matches?: number | null;
  nearest_fire_m?: number | null;
  scene_pre?: string | null;
  scene_post?: string | null;
  state?: string | null;
  centroid?: LonLat | null;
  res_m?: number | null;
};

export type ScarList = Envelope & {
  totals?: { clusters: number; area_ha: number; unseen_clusters: number; unseen_area_ha: number; checked_clusters?: number };
  returned?: number;
  window?: { first_pass: IsoTime | null; last_pass: IsoTime | null };
  method?: { label?: string; unseen_rule?: string; model_version?: string };
  features: FeatureCollection<AreaGeometry, ScarListProps>;
};

export type CaseImage = {
  date: string | null;
  datetime?: IsoTime | null;
  item_id: string;
  collection: string;
  truecolor_url?: string | null;
  swir_url?: string | null;
};

export type ScarCase = Envelope & {
  case: ScarListProps;
  imagery?: { bbox?: number[]; pre?: CaseImage | null; post?: CaseImage | null } | null;
  fires_nearby?: { distance_m?: number | null; explains_scar?: boolean }[];
};

// ---------------------------------------------------------------------------------------------
// GET /api/risk

export type RiskRow = {
  village: string;
  district: string;
  clusters: number;
  area_ha: number;
  risk_score: number;
  max_risk_score?: number;
  risk_weighted_ha?: number;
  village_inferred?: boolean;
};

export type Risk = Envelope & {
  as_of?: IsoTime;
  totals?: { clusters: number; area_ha: number };
  ranking?: RiskRow[];
  ranking_order?: string;
  method?: { note?: string; formula?: string };
  backtest?: {
    headline?: { auc?: number; tile?: string; burnt?: number; unburnt?: number; baselines?: { random?: { auc?: number } } } | null;
    all_pairs?: { pairs?: number; auc_weighted_mean?: number; auc_ci95?: [number, number] | null } | null;
  } | null;
  reason?: string | null;
};

// ---------------------------------------------------------------------------------------------
// GET /api/agent/examples

export type AgentExample = {
  id: string;
  kind?: string;
  question: string | null;
  answer: string | null;
  model?: string | null;
  started_at?: IsoTime | null;
  finished_at?: IsoTime | null;
  ok?: boolean;
  citations?: { marker: number | string; source: SourceRef }[] | null;
  verification?: { passed?: boolean; checked?: number; verified?: number; unverified?: unknown[] } | null;
  tools_used?: string[];
};

export type AgentExamples = Envelope & { examples?: AgentExample[] };

// ---------------------------------------------------------------------------------------------
// GET /api/imagery/featured

export type FeaturedFrame = {
  date: string;
  datetime?: IsoTime;
  item_id: string;
  collection: string;
  platform?: string;
  hazy?: boolean;
  truecolor_url: string;
  swir_url: string;
  width: number;
  height: number;
};

export type Featured = Envelope & {
  aoi?: {
    name?: string;
    bbox?: number[];
    center?: number[];
    size_km?: number[];
    district?: string;
    state?: string;
    chosen_value?: number;
    chosen_unit?: string;
    chosen_count?: number;
    chosen_since?: IsoTime;
  };
  frames?: FeaturedFrame[];
  reason?: string | null;
  stale?: boolean;
};

// ---------------------------------------------------------------------------------------------
// GET /api/fires/hours and GET /api/met/now (the few fields the moments read)

export type FireWindow = { start: string; end: string; count?: number; share?: number; satellites?: string[]; daynight?: 'D' | 'N' | string; minor?: boolean };
export type FireHours = Envelope & {
  days?: number;
  total?: number;
  daytime_share?: number | null;
  observed_windows_ist?: FireWindow[];
  unobserved_span_ist?: { start: string; end: string; hours?: number } | null;
};

export type MetNow = Envelope & { city?: string; t?: IsoTime; blh_m?: number | null };

// ---------------------------------------------------------------------------------------------
// GET /api/orbits/passes and GET /api/geostationary/diurnal (the blind-spot clock)

export type OrbitPass = {
  satellite: string;
  key?: string;
  instrument: string;
  start_utc: IsoTime;
  peak_utc?: IsoTime;
  end_utc: IsoTime;
  daynight?: string;
  min_ground_distance_km?: number;
};

export type OrbitPasses = Envelope & {
  site?: { lat: number; lon: number; name?: string };
  window?: { from_utc: IsoTime; to_utc: IsoTime };
  passes: OrbitPass[];
};

export type Diurnal = Envelope & {
  days?: number;
  since?: IsoTime;
  unit?: string;
  scans?: number;
  hourly_ist?: { hour: number; mean_count: number | null; scans?: number }[];
  peak_ist?: string | null;
  share_outside_polar_windows?: number | null;
  pixel_hours?: number | null;
  pixel_hours_outside_polar_windows?: number | null;
  /** IST times of day a polar fire satellite looked at the region on at least one day of the period
   *  (from its FIRMS detections, padded and merged across days). */
  polar_windows_ist?: { start: string; end: string; start_min: number; end_min: number; satellites?: string[] }[];
  polar_passes?: number | null;
  /** The spans between those windows (wrapping past midnight), longest first, with GK-2A's activity in each. */
  unobserved_gaps_ist?: { start: string; end: string; minutes: number; pixel_hours?: number | null }[];
};

// ---------------------------------------------------------------------------------------------
// GET /api/scars?bbox= (the outlines on the hero's frames)

export type ScarPolygon = { type: 'Polygon'; coordinates: number[][][] } | { type: 'MultiPolygon'; coordinates: number[][][][] };

export type FrameScars = Envelope & {
  totals?: { clusters: number; area_ha: number; unseen_clusters: number; unseen_area_ha: number; checked_clusters?: number };
  truncated?: boolean;
  window?: { first_pass: IsoTime | null; last_pass: IsoTime | null };
  features: { type: 'FeatureCollection'; features: { type: 'Feature'; id?: number | string; geometry: ScarPolygon | null; properties: ScarListProps }[] };
};
