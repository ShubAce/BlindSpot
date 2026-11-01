/**
 * Model-written text (an investigator answer, a bulletin) as plain words. Safe on the server and in
 * the browser.
 *
 *   plainText('Sangrur led with 412 fires [2, 3]. **Upper bound.**')   // "Sangrur led with 412 fires. Upper bound."
 */

/** A citation marker as the API writes it: "[2]", "[2, 3]", "[2-4]", with the space before it. */
const MARKERS = /[ \t]*\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]/g;

/** No citation markers (they number one run's sources) and no bold marks. Line breaks are kept. */
export const plainText = (text: string): string => text.replace(MARKERS, '').replace(/\*\*/g, '').trim();
