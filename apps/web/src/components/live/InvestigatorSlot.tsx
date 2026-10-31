'use client';

import { Investigator } from './investigator/Investigator';

/**
 * The investigator console's place in mission control (docs/build-logs/live.md, "Investigator
 * contract"). It sits in the screen grid's `console` area: a column to the right of the ranked list
 * on wide screens, and a full-width block under the panels below 1100px. The console itself lives in
 * ./investigator/ and drives the map through `useMapCommands()` (./map-commands.tsx).
 */
export function InvestigatorSlot() {
  return <Investigator />;
}
