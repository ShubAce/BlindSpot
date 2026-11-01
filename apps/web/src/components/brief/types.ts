/**
 * The field brief's shapes: GET /api/brief and GET /api/brief/{district}, as served
 * (docs/api-contract-notes.md, "Field brief"). Kept beside the brief's screens rather than in
 * lib/api.ts, like the case file's types. Fields the contract can leave empty are nullable here.
 */

import type { Envelope } from '@/lib/api';
import type { LangCode } from '@/lib/langs';

/** The days of Sentinel-2 passes a brief counts. `to` is the newest pass in the scar table, not
 *  today, so the page says "the 7 days of passes up to <to>", never "the last 7 days". */
export type BriefWindow = {
  /** UTC date, "2026-10-24" */
  from: string;
  /** UTC date, "2026-10-30" */
  to: string;
  days: number;
  newest_pass: string | null;
  anchor?: string;
  basis?: string;
};

/** The hand audit of candidate unseen burns: precision is the share that were real burns, so every
 *  count and area of them is an upper bound. */
export type BriefAudit = {
  precision: number | null;
  precision_ci95: [number, number] | null;
  audited: number | null;
  method?: string | null;
  date?: string | null;
  current?: boolean | null;
  reading?: string | null;
};

export type BriefRiskRun = {
  run_id: number | null;
  model: string | null;
  as_of: string | null;
  fields_ranked: number | null;
  top_tenth?: string | null;
};

/** How well the burn-risk order has picked the fields that burnt next, in the backtest. A rank says
 *  where to look first; it is never a probability. */
export type RankingReliability = {
  available: boolean;
  model?: string | null;
  computed_at?: string | null;
  strength?: string | null;
  timing_tested?: boolean | null;
  all_pairs?: {
    pairs?: number | null;
    auc_weighted_mean?: number | null;
    auc_ci95?: [number, number] | null;
    top_10pct?: {
      burnt_share?: number | null;
      burnt_share_ci95?: [number, number] | null;
      lift?: number | null;
      lift_ci95?: [number, number] | null;
      base_rate?: number | null;
    } | null;
  } | null;
  reading?: string | null;
};

export type BriefLinks = { brief: string; csv: string };

export type BriefDistrictRow = {
  district: string;
  state: string | null;
  unseen_clusters: number;
  unseen_area_ha: number;
  villages: number;
  newest_pass: string | null;
  harvested_fields: number;
  harvested_area_ha: number;
  top_tenth_fields: number;
  top_tenth_villages: number;
  links: BriefLinks;
};

/** GET /api/brief */
export type BriefIndexResponse = Envelope & {
  /** null before any scar is mapped; `reason` then says why */
  window: BriefWindow | null;
  label: string;
  districts: BriefDistrictRow[];
  districts_order?: string;
  totals: { districts: number; unseen_clusters: number; unseen_area_ha: number } | null;
  candidate_unseen_audit: BriefAudit | null;
  risk_run: BriefRiskRun | null;
  ranking_reliability: RankingReliability | null;
  notes: string[];
  reason: string | null;
};

export type BriefVillage = {
  /** null: a sub-district's scars with no census village within 3 km */
  village: string | null;
  subdistrict: string | null;
  unseen_clusters: number;
  unseen_area_ha: number;
  newest_pass: string | null;
  /** the village's largest candidate unseen burn */
  lat: number | null;
  lon: number | null;
  /** up to 5, largest first; each opens /case/<id> */
  case_ids: number[];
  maps_url: string | null;
  case_url: string | null;
};

export type BriefWatchRow = {
  village: string | null;
  subdistrict: string | null;
  fields: number;
  area_ha: number;
  top_tenth_fields: number;
  top_tenth_area_ha: number;
  /** false: no field of this village is in the run's top tenth; never present it as high risk */
  in_top_tenth: boolean;
  /** rank of the village's best field among risk_run.fields_ranked (1 = the highest) */
  best_rank: number | null;
  lat: number | null;
  lon: number | null;
  field_ids: number[];
  maps_url: string | null;
};

/** GET /api/brief/{district} */
export type DistrictBriefResponse = Envelope & {
  district: string;
  state: string | null;
  window: BriefWindow | null;
  label: string;
  totals: {
    unseen_clusters: number;
    unseen_area_ha: number;
    villages: number;
    newest_pass: string | null;
    harvested_fields: number;
    harvested_area_ha: number;
    top_tenth_fields: number;
    top_tenth_villages: number;
  };
  villages: BriefVillage[];
  villages_total: number;
  villages_order?: string;
  watchlist: BriefWatchRow[];
  watchlist_total: number;
  watchlist_in_top_tenth: number;
  watchlist_order?: string;
  candidate_unseen_audit: BriefAudit | null;
  risk_run: BriefRiskRun | null;
  ranking_reliability: RankingReliability | null;
  notes: string[];
  links: BriefLinks;
  reason: string | null;
};

/** A CSV's language: the API serves the brief's CSV in each of the site's languages. */
export type CsvLang = LangCode;
