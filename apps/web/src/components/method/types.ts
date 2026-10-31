/**
 * The validation and method payloads the method page reads, as served (docs/api-contract-notes.md).
 * Only the fields this page uses are typed; the rest of each response is tolerated and ignored.
 */

import type { Envelope, IsoTime } from '@/lib/api';
import type { ScarMethod, UnseenBurnsBlock } from '@/components/case/types';

export type ErrorStats = { n: number; mean_min: number | null; median_min: number | null; min_min: number | null; max_min: number | null; p95_abs_min: number | null; within_3_min_share: number | null };
export type OrbitValidation = Envelope & {
  days: number;
  window: { from_utc: IsoTime; to_utc: IsoTime };
  method: string;
  overall: ErrorStats | null;
  satellites: { satellite: string; instrument: string; detections: number; error_minutes: ErrorStats; cross_track_km_median?: number | null; tle_epoch?: IsoTime | null }[];
  note: string | null;
};

export type GeoGroup = { passes: number; hit_rate: number | null; n: number; hits?: number; false_alarm_rate: number | null; n_geo: number; geo_confirmed?: number; hit_rate_strong?: number | null; n_strong?: number; hits_strong?: number };
export type GeoDiurnal = Envelope & {
  days: number;
  since: IsoTime;
  unit: string;
  region: string[];
  scans: number;
  hourly_ist: { hour: number; mean_count: number | null; scans: number }[];
  polar_windows_ist: { start: string; end: string; start_min: number; end_min: number; satellites: string[] }[];
  polar_passes?: number;
  peak_ist: string | null;
  peak_mean_count: number | null;
  pixel_hours: number | null;
  pixel_hours_outside_polar_windows: number | null;
  share_outside_polar_windows: number | null;
  unobserved_gaps_ist: { start: string; end: string; minutes: number; pixel_hours: number }[];
  interpretation: string[];
  validation: {
    reference: string;
    period?: [IsoTime, IsoTime];
    algorithm?: string;
    method?: string;
    groups: Partial<Record<'viirs_all' | 'viirs_day' | 'viirs_night' | 'modis_aqua_day' | 'modis_terra' | 'all', GeoGroup>>;
  } | null;
  available?: boolean;
  reason?: string | null;
};

export type ScarsSummary = Envelope & {
  season: { since: string; clusters: number; area_ha: number; unseen_area_ha: number; unseen_share: number | null; last_pass: IsoTime | null; checked_area_ha?: number };
  coverage: { processed: number; skipped: number; failed: number; catalogued: number; latest_processed_pass: IsoTime | null; tiles: string[] } | null;
  method: ScarMethod | null;
};

export type Headline = Envelope & {
  unseen_burns?: UnseenBurnsBlock & {
    clusters?: number;
    area_ha?: number;
    unseen_area_ha?: number;
    unseen_clusters?: number;
    districts?: number;
    first_pass?: IsoTime;
    last_pass?: IsoTime;
    model_version?: string;
  };
};

export type Corr = { n: number; r: number | null; rho: number | null; lag_h: number; n_eff?: number | null; partial_r?: number | null };
export type CorrSet = { lag0: Corr | null; by_lag?: Corr[]; best_lag: Corr | null };
export type SmokeValidation = Envelope & {
  run_at: IsoTime;
  t0: IsoTime;
  hours: number;
  method: string;
  headline: string | null;
  notes?: string[];
  period?: { from: IsoTime; to: IsoTime; spinup_h?: number };
  inputs?: { fires?: number; scars?: number };
  median_r_cams?: number | null;
  median_r_ventilation_only?: number | null;
  median_r_smoke_per_km_mixing_depth?: number | null;
  cities: {
    city: string;
    stations?: number;
    hours_observed?: number;
    summary?: string;
    smoke_index?: CorrSet;
    smoke_per_km_mixing_depth?: CorrSet;
    ventilation_only?: CorrSet;
    cams_baseline?: CorrSet;
  }[];
};

/** What the method page keeps of /api/smoke/validation (the series are dropped on the server). */
export type SmokeCityRow = { city: string; model: number | null; modelLag: number | null; cams: number | null; ventilation: number | null; nEff: number | null; hours: number | null };

export type AucPair = { t0: IsoTime; t1: IsoTime; auc: number | null; base_rate: number | null; burnt: number; unburnt: number };
export type RiskMethod = Envelope & {
  as_of: IsoTime;
  totals: { clusters: number; area_ha: number; scored?: number };
  method: { formula: string; model: string; weights: Record<string, number>; factors: Record<string, string>; note: string } | null;
  backtest: {
    computed_at?: IsoTime;
    headline: {
      tile: string;
      t0: IsoTime;
      t1: IsoTime;
      gap_days?: number;
      burnt: number;
      unburnt: number;
      base_rate: number;
      auc: number;
      precision_at_k?: Record<string, number>;
      baselines?: Record<string, { auc: number; precision?: number }>;
      why_this_pair?: string;
    } | null;
    all_pairs: { pairs: number; auc_weighted_mean: number | null; auc_by_pair: AucPair[] } | null;
    timing_tested?: boolean;
  } | null;
  reason: string | null;
};

export type SourceRow = {
  key: string;
  label: string;
  kind: string;
  status: 'ok' | 'stale' | 'error' | 'never';
  last_success_at: IsoTime | null;
  cadence_minutes: number | null;
  docs_url: string | null;
  records_last?: number | null;
};
export type SourcesList = { generated_at: IsoTime; sources: SourceRow[] };

export type Featured = Envelope & {
  aoi: { name: string; district?: string; state?: string; chosen_value?: number; chosen_unit?: string; chosen_since?: IsoTime; size_km?: [number, number] } | null;
  frames: { date: string; datetime: IsoTime; platform?: string; item_id: string; truecolor_url: string; swir_url: string; width: number; height: number; hazy?: boolean; cloud_pct?: number | null }[];
  reason?: string | null;
};
