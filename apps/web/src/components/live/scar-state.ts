import type { ScarProps } from '@/lib/api';

/**
 * The three honest states of a scar, exactly as `scarState` in components/map/layers/scars.ts:
 * `unseen` is null until the fire-matching job has run on it, and an unchecked scar is never called
 * unseen. Repeated here only because that module imports deck.gl, which the parts of mission
 * control that paint before the map must not load.
 */
export type ScarState = 'unseen' | 'alerted' | 'unchecked';
export const scarState = (p: Pick<ScarProps, 'unseen'>): ScarState => (p.unseen === true ? 'unseen' : p.unseen === false ? 'alerted' : 'unchecked');
