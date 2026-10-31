'use client';

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';

import { CACHE_PREFIX, markSwitchedOff, onceIn, startConnectivity } from './connectivity';
import { OfflineNotice } from './OfflineNotice';
import './pwa.css';

/**
 * Offline support for the whole site, rendered once by the root layout. If the venue's Wi-Fi drops,
 * every page this browser has seen keeps working from the last data it had, and says so.
 *
 * - Registers the service worker (public/sw.js) in production builds only, once the page has loaded,
 *   so it never competes with the first paint. In development it removes any worker a production
 *   build left on this origin, which would otherwise serve old files.
 * - On a page loaded before the worker controlled it (a first visit, a hard reload), hands the worker
 *   the list of what the page loaded, so those answers are saved too (the boundaries, for one, are
 *   read once and never polled again).
 * - Kill switch: ?sw=off unregisters the worker, deletes its saved copies, reloads the page once so
 *   that no worker controls it, and keeps it off in this browser; ?sw=on allows it again.
 * - Renders the offline notice (OfflineNotice).
 */

const OFF_KEY = 'blindspot:offline-copies';
const OFF_RELOAD_KEY = 'blindspot:offline-off-reloaded-at';

export function OfflineSupport() {
  const router = useRouter();
  useEffect(() => startConnectivity(() => router.refresh()), [router]);
  useEffect(() => {
    void setUpWorker();
  }, []);
  return <OfflineNotice />;
}

async function setUpWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    const asked = new URLSearchParams(window.location.search).get('sw');
    if (asked === 'off') {
      writeOff(true);
      // the worker still controlling this page stops answering and saving at once
      navigator.serviceWorker.controller?.postMessage({ source: 'blindspot', type: 'off' });
      await removeWorkers();
      // Unregistered, the worker still controls this page, and it keeps "off" in memory only: Chrome
      // stops an idle worker after about 30 s and starts it afresh for the page's next request, which
      // it would then save again. One reload leaves the page with no worker (an unregistered one
      // takes no new page), so the reloaded page does not reload again; should a worker still claim
      // it, the 30 s guard ends the switch-off there instead of looping (and with storage blocked,
      // this tab cannot tell, so it does not reload).
      if (navigator.serviceWorker.controller && onceIn(OFF_RELOAD_KEY, 30_000, false)) {
        window.location.reload();
        return;
      }
      markSwitchedOff();
      // a save already under way when it stopped can land after the first sweep
      window.setTimeout(() => void removeWorkers().catch(() => undefined), 5_000);
      return;
    }
    if (asked === 'on') writeOff(false);
    if (process.env.NODE_ENV !== 'production') {
      await removeWorkers();
      return;
    }
    if (readOff()) return;
    await loaded();
    const uncontrolled = !navigator.serviceWorker.controller;
    await navigator.serviceWorker.register(workerUrl(), { scope: '/', updateViaCache: 'none' });
    if (uncontrolled) handOver(await navigator.serviceWorker.ready);
  } catch {
    // refused (a private window, blocked storage): the site works as before, online only
  }
}

/** /sw.js with the API's origin, which the worker saves answers from (NEXT_PUBLIC_API_URL is inlined
 *  at build time, so the address is the same on every load of a build). */
function workerUrl(): string {
  try {
    const api = new URL(process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').origin;
    return `/sw.js?api=${encodeURIComponent(api)}`;
  } catch {
    return '/sw.js';
  }
}

/** Tell the new worker what this page loaded before it was there; once more after 15 s for answers
 *  that were still arriving when it took over. */
function handOver(reg: ServiceWorkerRegistration) {
  const worker = reg.active;
  if (!worker) return;
  const sent = new Set<string>();
  const send = (page: string | null) => {
    const urls = loadedUrls().filter((u) => !sent.has(u));
    for (const u of urls) sent.add(u);
    if (page || urls.length) worker.postMessage({ source: 'blindspot', type: 'warm', page, urls });
  };
  send(window.location.href);
  window.setTimeout(() => send(null), 15_000);
}

function loadedUrls(): string[] {
  return performance
    .getEntriesByType('resource')
    .filter((e) => {
      // responseStatus is newer than the rest (Chrome 109); where present, failures are left out
      const status = (e as PerformanceResourceTiming & { responseStatus?: number }).responseStatus;
      return /^https?:/.test(e.name) && !(status != null && status >= 400);
    })
    .map((e) => e.name)
    .slice(0, 400);
}

async function removeWorkers() {
  const regs = await navigator.serviceWorker.getRegistrations();
  await Promise.all(regs.map((r) => r.unregister()));
  if ('caches' in window) {
    const names = (await caches.keys()).filter((n) => n.startsWith(CACHE_PREFIX));
    await Promise.all(names.map((n) => caches.delete(n)));
  }
}

/** Resolves on the load event, or after 10 s if a picture keeps it waiting. */
function loaded(): Promise<void> {
  return new Promise((resolve) => {
    if (document.readyState === 'complete') return resolve();
    window.addEventListener('load', () => resolve(), { once: true });
    window.setTimeout(resolve, 10_000);
  });
}

function readOff(): boolean {
  try {
    return window.localStorage.getItem(OFF_KEY) === 'off';
  } catch {
    return false;
  }
}

function writeOff(off: boolean) {
  try {
    if (off) window.localStorage.setItem(OFF_KEY, 'off');
    else window.localStorage.removeItem(OFF_KEY);
  } catch {
    // storage blocked: ?sw=off still unregisters, but the next page load registers again
  }
}
