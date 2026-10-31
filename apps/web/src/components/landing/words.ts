/**
 * Small wording helpers for the landing page. They turn API values into words and never supply a
 * value of their own: a missing input gives null, and the caller says what is missing.
 */

import type { CSSProperties } from 'react';

import { NBSP } from '@/lib/format';

/** "4 h 08 min 12 s", "8 min 12 s", "12 s": a countdown that ticks every second. */
export function countdown(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const p = (n: number) => String(n).padStart(2, '0'); // a clock's minutes and seconds; taste-skill-ignore
  if (h > 0) return `${h}${NBSP}h ${p(m)}${NBSP}min ${p(sec)}${NBSP}s`;
  if (m > 0) return `${m}${NBSP}min ${p(sec)}${NBSP}s`;
  return `${sec}${NBSP}s`;
}

/** "S2C_MSIL2A_…" or "S2B_T43…" or platform "S2C" -> "Sentinel-2C". Null when it is not one. */
export function sentinelName(itemOrPlatform: string | null | undefined): string | null {
  const m = itemOrPlatform?.match(/^S2([A-D])/i);
  return m ? `Sentinel-2${m[1].toUpperCase()}` : null;
}

/** "16:00-17:00" -> "16:00–17:00" (an en dash between times). */
export const span = (s: string | null | undefined): string | null => (s ? s.replace(/\s*-\s*/, '–') : null);

/** The first name of a census village list cut at 40 characters upstream:
 *  "Chamar Tibbi, Moranwali, Model Town ( R" -> "Chamar Tibbi". The full string goes in a title. */
export function firstPlace(name: string | null | undefined): string | null {
  if (!name) return null;
  const first = name.split(',')[0].replace(/\s*\(.*$/, '').trim();
  return first || null;
}

/** An answer's first lines, without citation markers' clutter at line ends kept intact. */
export function firstLines(text: string | null | undefined, n: number): string[] {
  if (!text) return [];
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .slice(0, n);
}

/** `style` for the n-th part of a Reveal block (its place in the entrance stagger). */
export const order = (i: number): CSSProperties => ({ ['--i' as string]: i });

/** An investigator answer as the model wrote it (a lead line, then "- " points, then a caveat) split for
 *  print: the lead, and n points without their list markers (those matching `prefer` first). The words
 *  are the model's own. */
export function answerParts(text: string | null | undefined, n: number, prefer?: RegExp, avoid?: RegExp): { lead: string | null; points: string[] } {
  const lines = (text ?? '')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);
  const isPoint = (l: string) => /^[-*•]\s+/.test(l);
  // Citation markers ("[4]") point to a source list the landing does not print: dropped.
  const plain = (l: string) => l.replace(/\*\*/g, '').replace(/\s*\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]/g, '');
  const lead = lines.find((l) => !isPoint(l)) ?? null;
  const points = lines
    .filter(isPoint)
    .map((l) => plain(l.replace(/^[-*•]\s+/, '')))
    // Points matching `avoid` are left out (a figure that would contradict one shown elsewhere).
    .filter((l) => !avoid || !avoid.test(l))
    // Points matching `prefer` first (in their own order), then the rest.
    .sort((a, b) => (prefer ? Number(prefer.test(b)) - Number(prefer.test(a)) : 0))
    .slice(0, n);
  return { lead: lead ? plain(lead) : null, points };
}

/** A source label without its parenthetical detail, for lists that must stay short:
 *  "NASA FIRMS active fires (VIIRS 375 m, MODIS 1 km; NRT)" -> "NASA FIRMS active fires". The full
 *  label stays in a title. */
export function shortLabel(label: string): string {
  return label.replace(/\s*\([^)]*\)/g, '').trim() || label;
}
