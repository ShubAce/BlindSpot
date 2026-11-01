import 'server-only';

import { serverGet } from '@/lib/api-server';

import { ALL_LIMIT, ROW_LIMIT } from './read';
import type { BriefIndexResponse, DistrictBriefResponse } from './types';

/**
 * The field brief's two reads, for Server Components. serverGet caches a good answer for 30 s and a
 * failure for a second, so the page and its metadata share one request when called with the same
 * arguments (the query objects are built here, in one order, for that reason).
 *
 * 15 s: an API instance's first brief scans the scar table and ranks every harvested field (1.7 to
 * 4.1 s measured), on top of a cold Python start after a deploy; the CDN answers in about 1 s after.
 */
const TIMEOUT_MS = 15_000;

export const getBriefIndex = (days: number) => serverGet<BriefIndexResponse>('/api/brief', { days }, TIMEOUT_MS);

/** `watchAll` asks for every row (the API's `limit` cuts both lists; the page still shows the first
 *  ROW_LIMIT villages, whose CSV has the rest): Patiala's 855 watchlist villages are 545 kB of JSON,
 *  so only a reader who asks for them gets them. */
export const getDistrictBrief = (district: string, days: number, watchAll = false) =>
  serverGet<DistrictBriefResponse>(`/api/brief/${encodeURIComponent(district)}`, { days, limit: watchAll ? ALL_LIMIT : ROW_LIMIT }, TIMEOUT_MS);
