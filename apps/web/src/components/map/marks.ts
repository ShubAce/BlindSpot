/**
 * Legend marks: a description of a map mark precise enough to draw it again at swatch size.
 * Built from the same style objects the layer factories use (palette.ts), so swatch and mark cannot
 * drift apart (app-screens.md section 6.4, "every swatch is the mark it explains").
 */

import type { RGBA } from './palette';

export type Mark =
  /** A filled circle, optionally ringed: fires, stations, satellite positions. */
  | { kind: 'dot'; fill: RGBA; stroke?: RGBA; strokeWidth?: number; radius: number }
  /** A ring with no fill: model air quality, risk at low zoom. */
  | { kind: 'ring'; stroke: RGBA; strokeWidth: number; radius: number }
  /** A square outline: a coarse sensor pixel (GK-2A). */
  | { kind: 'square'; stroke: RGBA; fill?: RGBA; strokeWidth: number }
  /** A filled shape: a burn scar. */
  | { kind: 'area'; fill: RGBA; stroke?: RGBA; strokeWidth?: number }
  /** An outlined shape with a faint fill: a field at risk. */
  | { kind: 'outline'; stroke: RGBA; strokeWidth: number; fill?: RGBA }
  /** A line: a ground track, a boundary. */
  | { kind: 'line'; color: RGBA; width: number; dash?: [number, number] }
  /** A moving trail that fades behind its head, drawn as three steps of its own alpha. */
  | { kind: 'trail'; color: RGBA; width: number }
  /** A swath band: a fill with edges. */
  | { kind: 'band'; fill: RGBA; edge: RGBA };

export type LegendItem = { label: string; mark: Mark; note?: string };
