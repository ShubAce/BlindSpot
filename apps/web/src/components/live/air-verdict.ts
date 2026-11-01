import { NAQI_PM25_EDGES } from '@/components/map/palette';

/**
 * The back-trajectory run's verdict hint in words, for the live map's air table and the landing's
 * Delhi line alike. The words name what the run found, where the air has been, never where a city's
 * smoke came from or how much of its PM2.5 is smoke.
 */
const VERDICT: Record<string, string> = {
  stubble_likely: 'Air crossed the burn belt',
  mixed: 'Some air crossed the belt',
  stubble_unlikely: 'Air missed the belt',
};

/** The hint in words; one this screen does not know yet reads as its own words. */
export const verdictText = (hint: string): string => VERDICT[hint] ?? hint.replace(/_/g, ' ');

/** Whether a known hint has words here (the landing shows no verdict line otherwise). */
export const knownVerdict = (hint: string | null | undefined): hint is string => hint != null && hint in VERDICT;

/**
 * The warning colour is kept for air that crossed the burn belt where the measured PM2.5 (µg/m³) is
 * also above CPCB's 'satisfactory' limit, the top of the second NAQI band (60 µg/m³).
 */
export const verdictWarns = (hint: string | null | undefined, pm25: number | null | undefined): boolean =>
  hint === 'stubble_likely' && pm25 != null && pm25 > NAQI_PM25_EDGES[1];
