/**
 * The mark of a failed server read, for the offline worker (public/sw.js).
 *
 * Next streams a page with status 200 before its sections have read the API, so a page whose read
 * failed (a cold API past serverGet's timeout, an outage) still arrives as a 200, with "Couldn't load
 * ..." in the failed section. Every server-rendered note of that kind carries data-load-failed="",
 * which shows in the HTML and in the App Router payload alike, and the worker never lets an answer
 * that has it replace a good copy it saved (keepRendered in sw.js).
 *
 * Put the attribute on the note itself: data-load-failed="" on a note that only a failure renders,
 * {...loadFailed(!r.ok)} on one that renders either way. <LoadFailed /> is for a failure that a client
 * component draws from its props (the payload carries a client component's props, not its markup), or
 * one that leaves nothing on the page to carry the attribute.
 */
export function LoadFailed() {
  return <span hidden data-load-failed="" />;
}

/** The attribute as props, present only when `failed`. Never data-load-failed={failed ? '' : undefined}:
 *  the payload keeps an undefined prop's name ("data-load-failed":"$undefined") on every good page. */
export const loadFailed = (failed: boolean): { 'data-load-failed'?: '' } => (failed ? { 'data-load-failed': '' } : {});
