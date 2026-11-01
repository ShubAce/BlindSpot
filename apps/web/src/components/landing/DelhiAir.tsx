'use client';

import Link from 'next/link';

import { knownVerdict, verdictText, verdictWarns } from '@/components/live/air-verdict';
import { PATHS } from '@/lib/api';
import { metricText, number, percent, relative } from '@/lib/format';
import { CADENCE, useApi, useNow } from '@/lib/live';

import { s } from './styles';
import type { Headline } from './types';


/**
 * Section 6's paragraph: Delhi's measured PM2.5 now, or, when every station is late (OpenAQ
 * republishes CPCB data 2-4 h behind), the last measured value with its age. Never the CAMS model or
 * the 24 h AQI in its place. Then where the air came from: the trace's verdict, named for what the
 * back-trajectories show (the air crossed the burn belt or not), the share of its last day over the
 * belt, and the plain limit: this is where the air has been, not how much of its PM2.5 is smoke. The
 * words and the warning tone are the live map's (components/live/air-verdict.ts): warning only for
 * air that crossed the belt where the measured PM2.5 is also above 60 µg/m³. The reason after the
 * verdict is the API's own.
 */
export function DelhiAir({ initial }: { initial: Headline | null }) {
  const head = useApi<Headline>(PATHS.headline, undefined, { refreshInterval: CADENCE.headline, fallbackData: initial ?? undefined });
  const now = useNow(30_000);
  const d = head.data?.delhi;

  let air: string;
  // the PM2.5 the warning tone is judged on: a fresh reading, or the last one when every station is late
  let pm: number | null = null;
  if (!head.data) {
    air = head.error ? 'Delhi’s air reading did not load just now.' : '';
  } else if (!d?.available) {
    air = d?.reason ? `Delhi’s air reading is unavailable: ${d.reason}.` : 'Delhi’s air reading is unavailable right now.';
  } else if (d.pm25 != null) {
    pm = d.pm25;
    air = `Delhi’s PM2.5 now: ${metricText('pm25', d.pm25)}, the median of ${number(d.n_stations)} stations${d.observed_at && now ? `, measured ${relative(d.observed_at, now)}` : ''}.`;
  } else if (d.last?.value != null) {
    pm = d.last.value;
    // Stations republished late (OpenAQ runs 2-4 h behind CPCB): the last measured value, with its age.
    const age = d.last.observed_at && now ? relative(d.last.observed_at, now) : d.last.age_minutes != null ? `${number(d.last.age_minutes)} min ago` : null;
    air = `No fresh reading from Delhi’s stations. The last${age ? `, ${age},` : ''} was PM2.5 ${metricText('pm25', d.last.value)}${d.last.n_stations ? ` across ${number(d.last.n_stations)} stations` : ''}.`;
  } else {
    air = 'No Delhi station has reported PM2.5 in the last day.';
  }

  // The trace is a model (back-trajectories on two weather models), and is called one. h24: the share
  // of the traced air's last 24 hours spent over the burn belt.
  const t = d?.smoke_trace;
  const belt = t?.share_over_burn_belt?.h24;
  const verdict = knownVerdict(t?.verdict_hint) ? verdictText(t.verdict_hint) : null;
  const warn = verdict != null && verdictWarns(t?.verdict_hint, pm);
  const traced =
    belt != null
      ? `traced back hour by hour, the air reaching Delhi spent ${percent(belt)} of its last day over Punjab and Haryana’s burning belt`
      : 'so say the back-trajectories from Delhi';

  return (
    <div aria-live="polite">
      <p className={s.replyText}>{air}</p>
      {verdict ? (
        <p className={s.replyTrace}>
          <strong className={warn ? 'lp-air-verdict lp-air-verdict-warn' : 'lp-air-verdict'}>{verdict}</strong>
          {`: ${traced}${t?.verdict_reason ? ` (${t.verdict_reason})` : ''}. That is where the air has been, not a measure of how much of its PM2.5 is smoke; local sources weigh too (see the `}
          <Link href="/method">method</Link>
          {').'}
        </p>
      ) : null}
    </div>
  );
}
