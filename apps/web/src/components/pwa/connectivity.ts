import { useSyncExternalStore } from 'react';

/**
 * The page's side of offline support: what the connection is doing and what the service worker
 * (public/sw.js) handed this page from its saved copies, in one small store read with
 * useConnectivity(). OfflineSupport (root layout) starts it once.
 *
 * Three sources:
 * - navigator.onLine and its online/offline events. False only when the device has no network at
 *   all: a laptop on venue Wi-Fi with no internet behind it still reports true.
 * - The Server-Timing entry the worker adds to a page load it answered from a saved copy:
 *   `blindspot-saved;desc="<offline|slow|missing>";dur=<saved at, epoch ms>`.
 * - The worker's messages: an API answer or a payload served from a saved copy ('saved', with when it
 *   was saved), and the first fresh answer after that ('live'). These catch the Wi-Fi-without-internet
 *   case that navigator.onLine misses.
 */

export type Reason = 'offline' | 'slow';

export type Connectivity = {
  /** navigator.onLine */
  online: boolean;
  /** This page's content came from a saved copy: its load or a soft navigation ('page'), or the
   *  offline screen is standing in for a page this browser never saved ('missing'). */
  doc: { kind: 'page' | 'missing'; reason: Reason; savedAt: number | null } | null;
  /** Answers served from saved copies since the network last answered this page. */
  saved: { reason: Reason; oldest: number | null } | null;
  /** When the network last answered this page: its load, or the worker's last fresh answer. */
  liveAt: number | null;
  /** ?sw=off just turned offline copies off in this browser. */
  switchedOff: boolean;
  /** The offline screen is showing; it explains all this itself. */
  screen: boolean;
};

/** The header the worker stamps on every saved copy (epoch ms). Keep equal to STAMP in public/sw.js. */
export const SAVED_AT_HEADER = 'x-blindspot-saved';
/** Every store the worker keeps starts with this. Keep equal to PREFIX in public/sw.js. */
export const CACHE_PREFIX = 'blindspot-';

// The server, and the first render in the browser (hydration), always see this: nothing to say.
const QUIET: Connectivity = { online: true, doc: null, saved: null, liveAt: null, switchedOff: false, screen: false };

let state: Connectivity = QUIET;
const listeners = new Set<() => void>();

function set(patch: Partial<Connectivity>) {
  const next = { ...state, ...patch };
  if ((Object.keys(patch) as (keyof Connectivity)[]).every((k) => Object.is(state[k], next[k]))) return;
  state = next;
  for (const l of listeners) l();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useConnectivity(): Connectivity {
  return useSyncExternalStore(
    subscribe,
    () => state,
    () => QUIET,
  );
}

/** The worker's mark on this page's own load, if it served it from a saved copy. */
function savedDocument(): Connectivity['doc'] {
  try {
    const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
    const mark = nav?.serverTiming?.find((t) => t.name === 'blindspot-saved');
    if (!mark) return null;
    return {
      kind: mark.description === 'missing' ? 'missing' : 'page',
      reason: mark.description === 'slow' ? 'slow' : 'offline',
      savedAt: mark.duration > 0 ? mark.duration : null,
    };
  } catch {
    return null;
  }
}

/** Whether the site itself answers right now: a HEAD, which the worker leaves alone (it answers GET
 *  only), and no-store keeps it out of the HTTP cache, so only the network can say yes. */
export async function siteAnswers(): Promise<boolean> {
  try {
    return (await fetch('/icon.svg', { method: 'HEAD', cache: 'no-store' })).ok;
  } catch {
    return false;
  }
}

let refreshPage: (() => void) | null = null;
let started = false;
let probing = false;

const REFRESHED_KEY = 'blindspot:offline-refreshed-at';

/**
 * The page's own content came from a saved copy and the network seems back: fetch the content again
 * (its server-rendered parts do not refresh by themselves; SWR panels revalidate on reconnect).
 *
 * Only once the site itself answers. A fresh-looking API answer can come from the browser's HTTP cache
 * (the API allows 60 s), and the online event only means the device has a network. Refreshing without
 * the site makes the router's request fail, and the router answers a failed refresh with a full page
 * load, which offline is the same saved copy again: a reload loop. So the site is asked first
 * (siteAnswers), and a refresh happens at most once a minute in this tab, whatever happens in between.
 *
 * Asked on the online event, on a fresh API answer, and every 30 s while a saved page shows on a
 * device that says it has a network: Wi-Fi without internet never fires the online event, and a page
 * without polling panels (the bulletin) gets no API answer to hear it from either.
 */
async function refreshWhenReachable() {
  if (probing || state.doc?.kind !== 'page') return;
  probing = true;
  // still out of reach: the saved copy stays on screen, and so does the notice
  const answers = await siteAnswers();
  probing = false;
  if (!answers || state.doc?.kind !== 'page') return;
  set({ doc: null });
  // with storage blocked it refreshes anyway: the site answered just now, which is the main guard
  if (onceIn(REFRESHED_KEY, 60_000, true)) refreshPage?.();
}

/** True at most once in `ms` for `key` in this tab (sessionStorage), so a step that refreshes or
 *  reloads the page cannot loop; `ifBlocked` is the answer when storage is refused. */
export function onceIn(key: string, ms: number, ifBlocked: boolean): boolean {
  try {
    const last = Number(window.sessionStorage.getItem(key)) || 0;
    if (Date.now() - last < ms) return false;
    window.sessionStorage.setItem(key, String(Date.now()));
    return true;
  } catch {
    return ifBlocked;
  }
}

/** Fresh answers again. A payload from the network means the page on screen is fresh too; an API
 *  answer only says the API answered. */
function fresh(pageIsFresh: boolean) {
  set({ saved: null, liveAt: Date.now(), ...(pageIsFresh && state.doc?.kind === 'page' ? { doc: null } : {}) });
  if (!pageIsFresh) void refreshWhenReachable();
}

function onMessage(e: MessageEvent) {
  const m = e.data as { source?: string; type?: string; kind?: string; reason?: string; savedAt?: unknown } | null;
  if (!m || m.source !== 'blindspot-sw') return;
  if (m.type === 'live') {
    fresh(m.kind === 'page');
    return;
  }
  if (m.type !== 'saved') return;
  const reason: Reason = m.reason === 'slow' ? 'slow' : 'offline';
  const at = typeof m.savedAt === 'number' && m.savedAt > 0 ? m.savedAt : null;
  const prev = state.saved;
  const oldest = prev?.oldest != null && at != null ? Math.min(prev.oldest, at) : (prev?.oldest ?? at);
  set({
    // one answer that could not reach the network outranks one that was only slow
    saved: { reason: prev?.reason === 'offline' ? 'offline' : reason, oldest },
    ...(m.kind === 'page' ? { doc: { kind: 'page' as const, reason, savedAt: at } } : {}),
  });
}

function onOnline() {
  set({ online: true, saved: null });
  void refreshWhenReachable();
}
const onOffline = () => set({ online: false });
// a page restored from the back-forward cache may have missed both events
const onShow = () => set({ online: navigator.onLine });

/** Start listening (once per page). `refresh` fetches the page's content again (router.refresh). */
export function startConnectivity(refresh: () => void): () => void {
  refreshPage = refresh;
  if (started) return () => undefined;
  started = true;
  const doc = savedDocument();
  set({ online: navigator.onLine, doc, liveAt: doc ? null : Math.round(performance.timeOrigin) });
  window.addEventListener('online', onOnline);
  window.addEventListener('offline', onOffline);
  window.addEventListener('pageshow', onShow);
  const sw = 'serviceWorker' in navigator ? navigator.serviceWorker : null;
  sw?.addEventListener('message', onMessage);
  // deliver the worker's messages now, not only once the document has finished parsing
  sw?.startMessages();
  const probe = window.setInterval(() => {
    if (navigator.onLine && document.visibilityState === 'visible') void refreshWhenReachable();
  }, 30_000);
  return () => {
    started = false;
    window.clearInterval(probe);
    window.removeEventListener('online', onOnline);
    window.removeEventListener('offline', onOffline);
    window.removeEventListener('pageshow', onShow);
    sw?.removeEventListener('message', onMessage);
  };
}

export const markSwitchedOff = () => set({ switchedOff: true });
export const markScreen = (on: boolean) => set({ screen: on });
