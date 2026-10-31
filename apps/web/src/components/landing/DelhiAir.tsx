'use client';

import { PATHS } from '@/lib/api';
import { metricText, number, percent, relative } from '@/lib/format';
import { CADENCE, useApi, useNow } from '@/lib/live';

import { s } from './styles';
import type { Headline } from './types';

// The smoke trace's verdict hint, in words. The reason after it is the API's own.
const VERDICT: Record<string, string> = {
  stubble_likely: 'points to the burning fields',
  mixed: 'gives a mixed answer',
  stubble_unlikely: 'points away from the burning fields',
};

/**
 * Section 6's paragraph: Delhi's measured PM2.5 now, or, when every station is late (OpenAQ
 * republishes CPCB data 2-4 h behind), the last measured value with its age. Never the CAMS model or
 * the 24 h AQI in its place. Then the smoke trace's verdict in the API's own words.
 */
export function DelhiAir({ initial }: { initial: Headline | null }) {
  const head = useApi<Headline>(PATHS.headline, undefined, { refreshInterval: CADENCE.headline, fallbackData: initial ?? undefined });
  const now = useNow(30_000);
  const d = head.data?.delhi;

  let air: string;
  if (!head.data) {
    air = head.error ? 'Delhi’s air reading did not load just now.' : '';
  } else if (!d?.available) {
    air = d?.reason ? `Delhi’s air reading is unavailable: ${d.reason}.` : 'Delhi’s air reading is unavailable right now.';
  } else if (d.pm25 != null) {
    air = `Delhi’s PM2.5 now: ${metricText('pm25', d.pm25)}, the median of ${number(d.n_stations)} stations${d.observed_at && now ? `, measured ${relative(d.observed_at, now)}` : ''}.`;
  } else if (d.last?.value != null) {
    // Stations republished late (OpenAQ runs 2-4 h behind CPCB): the last measured value, with its age.
    const age = d.last.observed_at && now ? relative(d.last.observed_at, now) : d.last.age_minutes != null ? `${number(d.last.age_minutes)} min ago` : null;
    air = `No fresh reading from Delhi’s stations. The last${age ? `, ${age},` : ''} was PM2.5 ${metricText('pm25', d.last.value)}${d.last.n_stations ? ` across ${number(d.last.n_stations)} stations` : ''}.`;
  } else {
    air = 'No Delhi station has reported PM2.5 in the last day.';
  }

  // The smoke trace is a model (back-trajectories on two weather models), and is called one.
  const t = d?.smoke_trace;
  const belt = t?.share_over_burn_belt?.h24;
  // Where the air came from first, then the verdict and its reason: the reason ("…or the air passed
  // only a little burning") speaks of the burning met on the way, which a high belt share does not
  // contradict, but read before the share it sounded as if the air had missed the belt.
  const verdict = t?.verdict_hint ? (VERDICT[t.verdict_hint] ?? `reads “${t.verdict_hint.replace(/_/g, ' ')}”`) : null;
  const trace = verdict
    ? `${belt != null ? ` Our smoke model traces ${percent(belt)} of the air reaching Delhi over the burning belt within a day, and` : ' Our smoke model'} ${verdict}${t?.verdict_reason ? `: ${t.verdict_reason}` : ''}.`
    : '';

  return (
    <p className={s.replyText} aria-live="polite">
      {air}
      {trace}
    </p>
  );
}
