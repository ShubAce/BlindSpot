/**
 * GET /api/bulletin/latest?lang=en|hi|pa as served (docs/api-contract-notes.md, "Agent and
 * bulletin"). Kept here rather than in lib/api.ts, whose older `BulletinResponse` other screens use.
 */

import type { ApiResult } from '@/lib/api-server';
import type { Envelope, IsoTime, SourceRef } from '@/lib/api';

export type BulletinLangCode = 'en' | 'hi' | 'pa';

export type BulletinVerification = {
  passed: boolean;
  checked: number;
  verified: number;
  unverified: string[];
  uncited: string[];
  bad_markers: (string | number)[];
  removed_sentences: string[];
  /** false when the text mixes in letters from another script: hide or warn (contract notes). */
  script_ok: boolean;
  foreign_script_words: string[];
  first_pass_unverified: string[];
};

export type Bulletin = Envelope & {
  /** The IST day the bulletin is for (YYYY-MM-DD). */
  date: string;
  lang: BulletinLangCode;
  model: string | null;
  run_id: string | null;
  title: string;
  summary: string;
  sections: { heading: string; body: string }[];
  citations: { marker: number | string; source: SourceRef }[];
  verification: BulletinVerification | null;
  /** Already in IST, e.g. "2026-10-31 04:58 IST". */
  data_as_of: string | null;
  available_langs: BulletinLangCode[];
  /** Additions: false when the text failed the script check; `reason` then says why in a sentence. */
  available?: boolean;
  reason?: string | null;
  generated_at: IsoTime;
};

export type BulletinSet = Record<BulletinLangCode, ApiResult<Bulletin>>;
