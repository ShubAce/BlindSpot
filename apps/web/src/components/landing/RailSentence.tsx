import { getFireHours, getHeadline } from './data';
import { span } from './words';

/**
 * The rail's one sentence, from the data: the week's longest stretch with no fire satellite over
 * Punjab (GET /api/fires/hours `unobserved_span_ist`), else GK-2A's evening peak (headline). No time is
 * typed here; with neither, the sentence says it in words.
 */
export async function RailSentence() {
  const [hours, head] = await Promise.all([getFireHours(), getHeadline()]);
  const gap = hours.ok ? hours.data.unobserved_span_ist : null;
  const peak = head.ok && head.data.gk2a_evening?.available ? head.data.gk2a_evening.peak_ist : null;
  if (gap?.start && gap.end) return <>{`No fire satellite looks from ${gap.start} to ${gap.end} IST.`}</>;
  if (peak) return <>{`Burning peaks at ${span(peak)} IST, between the satellites’ looks.`}</>;
  return <RailSentenceWords />;
}

/** While the data loads, or if it cannot: the same idea without a figure. */
export function RailSentenceWords() {
  return <>Fields burn between the satellites’ looks.</>;
}
