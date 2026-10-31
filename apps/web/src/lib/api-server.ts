import 'server-only';

import { cacheLife } from 'next/cache';

import { API_BASE, ApiError, apiGet, PATHS, type ApiErrorKind, type Query } from './api';
import type {
  AirLatestResponse,
  BulletinLang,
  BulletinResponse,
  FiresHoursResponse,
  FiresResponse,
  HeadlineResponse,
  ImageryFeaturedResponse,
  OrbitsTleResponse,
  PassesQuery,
  PassesResponse,
  RiskResponse,
  ScarCaseResponse,
  ScarsResponse,
  ScarsSummaryResponse,
  SourcesResponse,
} from './api';

/**
 * Server-side reads for first paint (Server Components). Never throws: a page renders
 * `result.ok ? <Data/> : <ErrorNote message={result.error.message}/>`.
 *
 * Caching (Next 16 Cache Components are on in next.config.ts): a success is kept for 30 s and a
 * failure for about a second. Both lifetimes expire in under 5 minutes, so Next leaves these reads
 * out of the build-time prerender ("dynamic hole") and runs them at request time: `next build` never
 * needs the API to be up, and no build bakes in old numbers. Wrap each component that awaits one in
 * <Suspense> with a skeleton of the real size.
 *
 * On the server the API may be reached on an internal address (API_INTERNAL_URL, e.g. the compose
 * service name); browsers always use NEXT_PUBLIC_API_URL.
 *
 * Hand the data to SWR in the client so it keeps polling:
 *   <SWRConfig value={{ fallback: { [swrKey(PATHS.headline)]: result.data } }}>…
 */

export type ServerError = { kind: ApiErrorKind | 'unknown'; status: number | null; message: string; path: string };
export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ServerError };

const SERVER_BASE = (process.env.API_INTERNAL_URL || API_BASE).replace(/\/+$/, '');

// 10 s: the first read after an API deploy meets an empty CDN and a cold Python function (numpy,
// skyfield and rasterio load before the route runs). Sections stream behind <Suspense>, so a slow
// read delays only its own section.
export async function serverGet<T>(path: string, query?: Query, timeoutMs = 10_000): Promise<ApiResult<T>> {
  'use cache';
  try {
    const data = await apiGet<T>(path, query, { timeoutMs, base: SERVER_BASE });
    cacheLife({ stale: 30, revalidate: 30, expire: 120 });
    return { ok: true, data };
  } catch (e) {
    // Keep a failure for a second only: the next visitor tries again.
    cacheLife({ stale: 30, revalidate: 1, expire: 30 });
    const err = e instanceof ApiError
      ? { kind: e.kind, status: e.status, message: e.userMessage, path: e.path }
      : { kind: 'unknown' as const, status: null, message: 'The page could not ask the API.', path };
    return { ok: false, error: err };
  }
}

/** The same endpoints as `api`, for Server Components. */
export const serverApi = {
  headline: () => serverGet<HeadlineResponse>(PATHS.headline),
  sources: () => serverGet<SourcesResponse>(PATHS.sources),
  fires: (q: { hours?: number } = {}) => serverGet<FiresResponse>(PATHS.fires, q, 15_000),
  firesHours: (q: { days?: number; region?: string } = {}) => serverGet<FiresHoursResponse>(PATHS.firesHours, q),
  orbitsTle: () => serverGet<OrbitsTleResponse>(PATHS.orbitsTle),
  orbitsPasses: (q: PassesQuery) => serverGet<PassesResponse>(PATHS.orbitsPasses, q),
  scars: (q: { unseen?: boolean; limit?: number; since?: string } = {}) => serverGet<ScarsResponse>(PATHS.scars, q, 15_000),
  scarsSummary: (q: { since?: string } = {}) => serverGet<ScarsSummaryResponse>(PATHS.scarsSummary, q),
  scarCase: (id: string | number) => serverGet<ScarCaseResponse>(PATHS.scarCase(id)),
  risk: (q: { limit?: number; district?: string } = {}) => serverGet<RiskResponse>(PATHS.risk, q, 15_000),
  airLatest: () => serverGet<AirLatestResponse>(PATHS.airLatest),
  imageryFeatured: () => serverGet<ImageryFeaturedResponse>(PATHS.imageryFeatured),
  bulletinLatest: (lang: BulletinLang = 'en') => serverGet<BulletinResponse>(PATHS.bulletinLatest, { lang }),
};
