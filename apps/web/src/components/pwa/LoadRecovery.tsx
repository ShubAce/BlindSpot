'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useTransition } from 'react';

import { atMost, siteAnswers, useConnectivity } from './connectivity';

/**
 * Sections that failed on the server heal by themselves. A server read that met a cold API (past
 * serverGet's timeout) leaves its section saying "… did not load", with data-load-failed on the note
 * (LoadFailed.tsx), and nothing asked the API again until someone pressed Retry. This part, rendered
 * once by the root layout, watches for those marks and, while it sees one, asks the server for the
 * page's content again (router.refresh: the page stays where it is, only its server parts redraw)
 * 4 s, 12 s and 30 s after it first saw it. It stops as soon as no mark is left, after the third try
 * on a page, and on leaving the page. It shows nothing: a section that recovers simply fills in, and
 * one that does not keeps its own note and Retry.
 *
 * Once a refresh has left no mark, it asks the service worker to save this address again ('warm',
 * public/sw.js), so the offline copy is no longer the failed first load.
 *
 * Why three, so far apart: a failed read is cached for about a second and then served once more
 * while the server asks again in the background (lib/api-server.ts), so the first refresh can still
 * bring the failure back and start the fresh read that the second one picks up; 30 s covers an API
 * that was starting from cold.
 *
 * It never fights offline support:
 * - It waits while the device has no network, and while the page on screen is a saved copy or the
 *   offline screen (connectivity.ts): there OfflineSupport refreshes the page itself once the site
 *   answers, at most once a minute.
 * - It asks the site first (siteAnswers, a HEAD the worker leaves alone). The router answers a
 *   refresh that cannot reach the site with a full page load, which would start this all over; so it
 *   only refreshes a site that answers, and at most six times in three minutes per address in this
 *   tab (sessionStorage), whatever reloads happen in between.
 * - A Retry link is a full page load: a new visit, with its own three tries (within that cap).
 * - It waits for a refresh still under way, and while the tab is hidden.
 *
 * Marks inside a screen React keeps hidden (mission control in <Activity> after leaving /live) are
 * not this page's and are left alone.
 */

const DELAYS = [4_000, 12_000, 30_000];
const LOG_KEY = 'blindspot:load-recovery';
const CAP = 6;
const CAP_WINDOW = 180_000;
// how often, at most, DOM changes make it look for marks again
const LOOK_EVERY = 800;

export function LoadRecovery() {
  const router = useRouter();
  const c = useConnectivity();
  const eligible = c.online && c.doc == null && !c.screen;
  const [pending, startTransition] = useTransition();

  // what the timers read when they fire; `poke` looks again when either changes
  const now = useRef({ eligible, pending });
  const poke = useRef<() => void>(() => undefined);
  useEffect(() => {
    now.current = { eligible, pending };
    poke.current();
  }, [eligible, pending]);

  useEffect(() => {
    let visit = window.location.pathname;
    let tries = 0;
    let since = 0; // when this page's first mark was seen
    let timer = 0;
    let look = 0;
    let healing = false; // a refresh went out on this page: once it lands without a mark, resave it

    const reset = () => {
      visit = window.location.pathname;
      tries = 0;
      since = 0;
      healing = false;
      window.clearTimeout(timer);
      timer = 0;
    };
    const check = () => {
      if (window.location.pathname !== visit) reset();
      if (healing && now.current.eligible && !now.current.pending && !hasFailedMark()) {
        healing = false;
        resavePage();
      }
      if (timer || tries >= DELAYS.length || !hasFailedMark()) return;
      if (!since) since = Date.now();
      timer = window.setTimeout(() => void fire(), Math.max(500, since + DELAYS[tries] - Date.now()));
    };
    const fire = async () => {
      timer = 0;
      if (window.location.pathname !== visit) return check();
      // not now: a change of any of these looks again (poke, visibilitychange)
      if (!now.current.eligible || now.current.pending || document.visibilityState !== 'visible') return;
      if (!hasFailedMark()) {
        since = 0;
        return check(); // resaves the page if a refresh healed it
      }
      tries += 1;
      const page = visit;
      if (!(await siteAnswers()) || window.location.pathname !== page || !atMost(`${LOG_KEY}:${page}`, CAP, CAP_WINDOW, true)) return check();
      healing = true;
      startTransition(() => router.refresh());
      // the next look is due at the next delay; by then the refreshed content has usually landed
      check();
    };
    poke.current = check;

    // this page's tries are spent: nothing to look for until the address changes
    const soon = () => {
      if (look || (tries >= DELAYS.length && window.location.pathname === visit)) return;
      look = window.setTimeout(() => {
        look = 0;
        check();
      }, LOOK_EVERY);
    };
    // only changes that can bring a mark: a mark set, or nodes added that are or hold one
    const seen = new MutationObserver((records) => {
      if (records.some(bringsMark)) soon();
    });
    seen.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-load-failed'] });
    document.addEventListener('visibilitychange', check);
    check();
    return () => {
      poke.current = () => undefined;
      seen.disconnect();
      document.removeEventListener('visibilitychange', check);
      window.clearTimeout(timer);
      window.clearTimeout(look);
    };
  }, [router]);

  return null;
}

const MARK = '[data-load-failed]';

/** Ask the worker controlling this page, if any, to fetch and save this address again. */
function resavePage() {
  try {
    navigator.serviceWorker?.controller?.postMessage({ source: 'blindspot', type: 'warm', page: window.location.href });
  } catch {
    // reading navigator.serviceWorker can throw where storage is blocked (Firefox): nothing to save
  }
}

function bringsMark(r: MutationRecord): boolean {
  if (r.type === 'attributes') return true;
  for (const n of r.addedNodes) if (n instanceof Element && (n.matches(MARK) || n.querySelector(MARK))) return true;
  return false;
}

/** A failure mark that belongs to the page on screen. */
function hasFailedMark(): boolean {
  for (const el of document.querySelectorAll(MARK)) if (!inHiddenScreen(el)) return true;
  return false;
}

/** Inside a subtree React hides (an <Activity> in hidden mode sets display: none !important on it). */
function inHiddenScreen(el: Element): boolean {
  for (let n: Element | null = el; n; n = n.parentElement) {
    if (n instanceof HTMLElement && n.style.display === 'none' && n.style.getPropertyPriority('display') === 'important') return true;
  }
  return false;
}
