/**
 * The case file's exact shape: GET /api/scars/{id} as served (docs/api-contract-notes.md, "Burn
 * scars"). Kept beside the case screen rather than in lib/api.ts, whose older `ScarCaseResponse` is
 * shared with other screens; every field the contract marks as possibly missing is optional here.
 */

import type { Envelope, IsoTime, LonLat, PolygonGeometry, MultiPolygonGeometry } from '@/lib/api';

export type CaseImageryPass = {
  date: string;
  datetime: IsoTime;
  item_id: string;
  collection: string;
  cloud_pct: number | null;
  truecolor_url: string | null;
  swir_url: string | null;
  nbr_url: string | null;
};

export type CaseEvidence = {
  n_px?: number | null;
  d_b8a?: number | null;
  d_b11?: number | null;
  d_b12?: number | null;
  bais2_post?: number | null;
  tbi_post?: number | null;
  ndvi_pre?: number | null;
  ndvi_post?: number | null;
};

export type CaseProps = {
  id: number;
  area_ha: number;
  /** The clear Sentinel-2 pass before the burn. */
  burn_after: IsoTime;
  /** The clear Sentinel-2 pass that shows the scar. */
  burn_before: IsoTime;
  district: string | null;
  subdistrict: string | null;
  village: string | null;
  village_km: number | null;
  state: string | null;
  centroid: LonLat | null;
  dnbr: number | null;
  nbr_post: number | null;
  confidence: number | null;
  /** true: no fire alert explains it; false: one does; null: not checked against the alerts yet. */
  unseen: boolean | null;
  /** The API's own label for the scar, used verbatim. */
  label: string;
  fire_matches: number | null;
  nearest_fire_m: number | null;
  scene_pre: string | null;
  scene_post: string | null;
  gap_days: number | null;
  hazy: boolean | null;
  res_m: number | null;
  mgrs_tile: string | null;
  collection: string | null;
  evidence?: CaseEvidence | null;
  matched_at?: IsoTime | null;
  model_version?: string | null;
  created_at?: IsoTime | null;
  geometry?: PolygonGeometry | MultiPolygonGeometry | null;
};

export type CaseFire = {
  id: number;
  source: string;
  satellite: string;
  instrument: string;
  acq_utc: IsoTime;
  lat: number;
  lon: number;
  confidence: string | null;
  frp_mw: number | null;
  daynight?: string | null;
  distance_m: number;
  match_radius_m: number;
  in_window: boolean;
  explains_scar: boolean;
};

export type CaseWeatherPoint = {
  t: IsoTime;
  wind_10m_kmh: number | null;
  wind_from_deg: number | null;
  blh_m: number | null;
  temp_2m_c: number | null;
  precip_mm: number | null;
};

export type CaseWeather = {
  available: boolean;
  reason?: string | null;
  model?: string | null;
  window?: { from: IsoTime; to: IsoTime } | null;
  hours?: number | null;
  covered_hours?: number | null;
  afternoon_wind_10m_kmh_mean?: number | null;
  afternoon_blh_m_mean?: number | null;
  rain_mm_total?: number | null;
  series?: CaseWeatherPoint[];
};

export type ScarAuditBlock = { audited?: number; burnt?: number; precision?: number | null; precision_ci95?: [number, number] | null };

export type ScarMethod = {
  model_version?: string;
  summary?: string;
  label?: string;
  unseen_rule?: string;
  audit?: {
    date?: string;
    method?: string;
    known_lookalikes?: string;
    lookalikes_removed?: string;
    candidate_unseen?: ScarAuditBlock;
    firms_confirmed?: ScarAuditBlock;
    recall_vs_firms?: {
      viirs_detections?: number;
      scar_same_window?: number;
      scar_any_window?: number;
      recall_same_window?: number | null;
      recall_same_window_ci95?: [number, number] | null;
      note?: string;
    };
    current?: boolean;
  };
};

export type CaseFile = Envelope & {
  case: CaseProps;
  imagery: { bbox: [number, number, number, number] | null; pre: CaseImageryPass | null; post: CaseImageryPass | null } | null;
  fires_nearby: CaseFire[];
  weather_at_burn: CaseWeather | null;
  method?: ScarMethod | null;
};

/** /api/stats/headline, the block this screen reads (the audit precision of candidate unseen burns). */
export type UnseenBurnsBlock = {
  available: boolean;
  reason?: string | null;
  label?: string;
  precision?: number | null;
  precision_ci95?: [number, number] | null;
  precision_n?: number | null;
  unseen_share?: number | null;
  note?: string | null;
};
export type HeadlineAudit = Envelope & { unseen_burns?: UnseenBurnsBlock };

/** /api/geo/boundaries?level=state: the Survey of India state polygons for the locator. */
export type StateBoundaries = Envelope & {
  attribution?: string;
  features: { type: 'FeatureCollection'; features: { type: 'Feature'; geometry: PolygonGeometry | MultiPolygonGeometry; properties: { name: string } }[] };
};
