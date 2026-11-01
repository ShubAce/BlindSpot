import type { SourceStatus } from '@/lib/api';

/** The fields of a GET /api/sources row that tell two registrations of one upstream apart. */
type SourceLike = Pick<SourceStatus, 'key' | 'docs_url' | 'last_success_at' | 'cadence_minutes'>;

/**
 * One row per upstream. The API registers some upstreams under two keys (the Sentinel-2 COGs on AWS,
 * as `s2_cog` and `s2_cog_e84`, share one docs address); the older key goes stale while the newer one
 * answers, so every count of sources read one short and named a stale source that is not. Rows with
 * the same docs address merge into the one with the newest good data, which keeps its own status,
 * label and last good time; a cadence missing there comes from its twin.
 */
export function onePerUpstream<T extends SourceLike>(rows: readonly T[]): T[] {
  const groups = new Map<string, T[]>();
  for (const r of rows) {
    const k = r.docs_url ?? `key:${r.key}`;
    groups.set(k, [...(groups.get(k) ?? []), r]);
  }
  return [...groups.values()].map((g) => {
    if (g.length === 1) return g[0];
    const t = (r: T) => (r.last_success_at ? Date.parse(r.last_success_at) : -Infinity);
    const best = g.reduce((a, b) => (t(b) > t(a) ? b : a));
    return { ...best, cadence_minutes: best.cadence_minutes ?? g.find((r) => r.cadence_minutes != null)?.cadence_minutes ?? null };
  });
}
