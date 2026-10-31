/*
 * BlindSpot's service worker. When the network goes (the venue's Wi-Fi drops), every page this
 * browser has already seen keeps working from the last data it had, and the page says so (the notice
 * in src/components/pwa). Hand-written, no Workbox: one file, served from public/ as /sw.js.
 * src/components/pwa/OfflineSupport.tsx registers it in production builds only, passing the API's
 * origin in the script URL (/sw.js?api=https://...), so this file needs no build step.
 *
 * What is kept, and how. Each store is a Cache named blindspot-<store>-<version>.
 *
 *   store    holds                                       strategy
 *   core     /offline and the files it needs             saved at install, never trimmed
 *   pages    page loads (HTML)                           network first; the saved copy after 5 s or
 *                                                        with no network; else the /offline screen
 *   rsc      App Router payloads (RSC: 1 or ?_rsc=)      network first, 5 s; the saved copy for the
 *                                                        same request (Vary respected); else a 503,
 *                                                        on which the router loads the page in full
 *   static   /_next/static/ (content-hashed names)       cache first
 *   assets   same-origin pictures, icons, the manifest   the saved copy at once, refreshed behind it
 *   api      BlindSpot API GETs                          network first; the saved copy after 8 s or
 *                                                        with no network
 *   imagery  /api/imagery/render and /tiles, NASA GIBS   cache first (a URL is one picture), aged out
 *   basemap  OpenFreeMap and EOX: styles, tiles, fonts,  cache first, aged out (a style after a day)
 *            sprites
 *
 * Only good answers are saved: status 200, not redirected, never an error. A picture the page asks
 * for without CORS (a plain <img>) is fetched again with CORS so its status can be read; if the host
 * refuses, the original request goes through and nothing is saved (an opaque answer could be an
 * error, and Chrome counts each one as megabytes of quota). Every store has a cap, trimmed oldest
 * first. Never touched: any method but GET, the investigator's live questions (/api/agent/ask and
 * anything asking for text/event-stream) and its budget (/api/agent/status), /api/health, range
 * requests, and every origin not named here.
 *
 * Telling the page: a page served from a saved copy carries a Server-Timing entry
 * `blindspot-saved;desc="<offline|slow|missing>";dur=<saved at, epoch ms>`; an API answer or a
 * payload served from a saved copy is posted to the page that asked
 * ({source: 'blindspot-sw', type: 'saved', ...}), and the next fresh one ({type: 'live'}).
 *
 * Kill switch: open any page with ?sw=off. This worker deletes its caches and unregisters, and the
 * page does the same and keeps it off in that browser until a page is opened with ?sw=on.
 */

// Bump SHELL with a deploy whose pages or code must not be served from older saved copies, and
// whenever this file changes what or how it saves: the next visit installs this worker, which
// deletes every store of another version. DATA covers the API's answers, pictures and map tiles,
// which do not change with the site's code; bump it only when old answers would mislead a newer page.
const SHELL = '2026-11-01.1';
const DATA = '2026-11-01.1';

const PREFIX = 'blindspot-';
const DAY = 24 * 60 * 60 * 1000;
const STORE = {
  core: { name: `${PREFIX}core-${SHELL}`, max: 0 }, // 0: never trimmed
  pages: { name: `${PREFIX}pages-${SHELL}`, max: 60 },
  rsc: { name: `${PREFIX}rsc-${SHELL}`, max: 150 },
  static: { name: `${PREFIX}static-${SHELL}`, max: 400 },
  assets: { name: `${PREFIX}assets-${SHELL}`, max: 120 },
  api: { name: `${PREFIX}api-${DATA}`, max: 250 },
  imagery: { name: `${PREFIX}imagery-${DATA}`, max: 300 },
  basemap: { name: `${PREFIX}basemap-${DATA}`, max: 400 },
};

/** A page or a payload waits this long for the network when a saved copy exists (a cold server). */
const PAGE_WAIT = 5000;
/** An API answer waits longer: a cold Python function on the API can take a few seconds. */
const API_WAIT = 8000;
const OFFLINE_PATH = '/offline';
/** When a copy was saved (epoch ms), on every saved response. OfflineScreen.tsx reads it too. */
const STAMP = 'x-blindspot-saved';

const API_ORIGINS = new Set(['https://blindspot-api-azure.vercel.app']);
try {
  const fromPage = new URL(self.location.href).searchParams.get('api');
  if (fromPage) API_ORIGINS.add(new URL(fromPage).origin);
} catch {
  // a malformed ?api= leaves the production API as the only one
}

// The investigator's live work and the API's own health: always the network, never a saved answer.
const NEVER_API = /^\/api\/(health|agent\/ask|agent\/status)(\/|$)/;
// Stored, verified investigator runs are fixed once written: safe to replay from a saved copy.
const KEPT_AGENT = /^\/api\/agent\/(examples|runs\/)/;

// ---------------------------------------------------------------------------------------------
// lifecycle

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      try {
        await saveDocument(OFFLINE_PATH, STORE.core, STORE.core);
      } catch {
        // no network at install: missing pages get a plain fallback until a later visit saves /offline
      }
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const wanted = new Set(Object.values(STORE).map((s) => s.name));
      const old = (await caches.keys()).filter((n) => n.startsWith(PREFIX) && !wanted.has(n));
      await Promise.all(old.map((n) => caches.delete(n)));
      if (self.registration.navigationPreload) {
        try {
          await self.registration.navigationPreload.enable();
        } catch {
          // not supported here: navigations simply start once the worker is running
        }
      }
      await self.clients.claim();
      // A new version dropped the old saved pages: save the pages open right now again, so they
      // still open offline (the files they load later are saved as they load).
      if (old.some((n) => n.startsWith(`${PREFIX}pages-`))) {
        const open = await self.clients.matchAll({ type: 'window' });
        await runLimited(
          open
            .filter((c) => isSaveablePage(c.url))
            .slice(0, 5)
            .map((c) => () => saveDocument(c.url, STORE.pages, STORE.static)),
          2,
        );
      }
      await Promise.all(Object.values(STORE).filter((s) => s.max).map(async (s) => trim(await caches.open(s.name), s.max)));
    })(),
  );
});

self.addEventListener('message', (event) => {
  const msg = event.data;
  if (!msg || msg.source !== 'blindspot') return;
  // A page that loaded before this worker controlled it (a first visit, a hard reload) lists what it
  // loaded, so those answers are saved too.
  if (msg.type === 'warm' && !off) event.waitUntil(warm(typeof msg.page === 'string' ? msg.page : null, Array.isArray(msg.urls) ? msg.urls : []));
  // The page's own kill switch (?sw=off reached it without passing through this worker).
  if (msg.type === 'off') event.waitUntil(turnOff());
});

// ---------------------------------------------------------------------------------------------
// routing

// After ?sw=off this worker is unregistered but still controls the pages already open until they
// navigate away; from then on it neither answers nor saves anything, so nothing comes back.
let off = false;

self.addEventListener('fetch', (event) => {
  if (off) return;
  const handle = routeFor(event.request);
  if (handle) event.respondWith(handle(event));
});

function routeFor(request) {
  if (request.method !== 'GET' || request.headers.has('range')) return null;
  if ((request.headers.get('accept') || '').includes('text/event-stream')) return null;
  // DevTools can send these; fetch() refuses them
  if (request.cache === 'only-if-cached' && request.mode !== 'same-origin') return null;
  let url;
  try {
    url = new URL(request.url);
  } catch {
    return null;
  }
  if (url.origin === self.location.origin) {
    if (request.mode === 'navigate') return url.searchParams.get('sw') === 'off' ? switchOff : page;
    if (request.headers.get('rsc') === '1' || url.searchParams.has('_rsc')) return payload;
  }
  const store = storeFor(url);
  return store ? HANDLERS[store] : null;
}

/** Which store a subresource belongs to (null: not handled). Navigations and payloads are routed above. */
function storeFor(url) {
  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith('/_next/static/')) return 'static';
    if (isSameOriginAsset(url.pathname)) return 'assets';
    return null;
  }
  if (API_ORIGINS.has(url.origin)) {
    const p = url.pathname;
    if (!p.startsWith('/api/') || NEVER_API.test(p)) return null;
    if (p.startsWith('/api/agent/') && !KEPT_AGENT.test(p)) return null;
    return /^\/api\/imagery\/(render|tiles)(\/|$)/.test(p) ? 'imagery' : 'api';
  }
  if (url.hostname === 'tiles.openfreemap.org' || url.hostname === 'tiles.maps.eox.at') return 'basemap';
  if (/^gibs(-[a-z])?\.earthdata\.nasa\.gov$/.test(url.hostname)) return 'imagery';
  return null;
}

function isSameOriginAsset(path) {
  return (
    path.startsWith('/_next/image') ||
    path.startsWith('/icons/') ||
    path.startsWith('/photos/') ||
    path === '/apple-icon' ||
    path === '/manifest.webmanifest' ||
    /\.(?:png|jpe?g|webp|avif|gif|svg|ico)$/i.test(path)
  );
}

const HANDLERS = {
  // content-hashed names: a saved copy is always right. ?dpl= (Vercel's deployment marker) is ignored.
  static: (event) => cacheFirst(event, STORE.static, { also: STORE.core, ignoreSearch: true }),
  assets: (event) => savedThenRefresh(event, STORE.assets),
  api: (event) => networkFirst(event, STORE.api, API_WAIT),
  imagery: (event) => cacheFirst(event, STORE.imagery, { maxAge: 30 * DAY, byUrl: true }),
  basemap: (event) => cacheFirst(event, STORE.basemap, { maxAge: basemapAge(event.request.url), byUrl: true }),
};

/** A style or TileJSON names the current tile set, so it is refreshed daily; tiles, glyphs and
 *  sprites under a versioned URL keep for a month. */
function basemapAge(href) {
  const p = new URL(href).pathname;
  return /\.(?:pbf|mvt|png|jpe?g|webp)$/i.test(p) || p.startsWith('/fonts/') || p.startsWith('/sprites/') ? 30 * DAY : DAY;
}

// ---------------------------------------------------------------------------------------------
// strategies

/** A page load: network first (with the navigation preload, when the browser started one). */
async function page(event) {
  const url = event.request.url;
  const answer = saving(event, fromNetwork(event), STORE.pages, (res) => isHtml(res) && isSaveablePage(url), url);
  const saved = await savedPage(url);
  if (!saved) {
    try {
      return await answer;
    } catch {
      return offlineScreen();
    }
  }
  const first = await within(answer, PAGE_WAIT);
  if (first.res) return first.res;
  // the network answer, if it comes, still replaces the saved copy (saving() runs on)
  return marked(saved, first.late ? 'slow' : 'offline');
}

/** An App Router payload (a soft navigation, a refresh, a prefetch): network first. A saved payload
 *  is used only for the very same request: the response varies on the router-state headers, which
 *  cache.match compares, and ?_rsc= is a hash of them. */
async function payload(event) {
  const request = event.request;
  const prefetch = request.headers.has('next-router-prefetch') || request.headers.has('next-router-segment-prefetch');
  const answer = saving(event, fetch(request), STORE.rsc, isFlight, request);
  const saved = await lookup(STORE.rsc, request);
  if (!saved) {
    try {
      const res = await answer;
      if (!prefetch && res.ok) tellLive(event, 'page');
      return res;
    } catch {
      // Not a network error: on a non-200 the router quietly loads the page in full instead, and that
      // load gets the saved page or the offline screen. (On a rejected fetch it logs an error first.)
      return new Response('', { status: 503, statusText: 'Offline', headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } });
    }
  }
  const first = await within(answer, PAGE_WAIT);
  if (first.res) {
    if (!prefetch && first.res.ok) tellLive(event, 'page');
    return first.res;
  }
  if (!prefetch) tellSaved(event, 'page', first.late ? 'slow' : 'offline', saved);
  return saved;
}

/** An API answer: network first; the saved copy when the network fails or is slower than `wait`. */
async function networkFirst(event, store, wait) {
  const request = event.request;
  const answer = saving(event, fetch(request), store, isOk, request);
  const saved = await lookup(store, request, { ignoreVary: true });
  if (!saved) {
    try {
      const res = await answer;
      if (res.ok) tellLive(event, 'api');
      return res;
    } catch {
      // nothing saved for this address: the page states its own error (lib/api.ts, "not reachable")
      return Response.error();
    }
  }
  const first = await within(answer, wait);
  if (first.res) {
    if (first.res.ok) tellLive(event, 'api');
    return first.res;
  }
  tellSaved(event, 'api', first.late ? 'slow' : 'offline', saved);
  return saved;
}

/** A file whose address names its content: the saved copy if young enough, else the network; with
 *  no network, an aged copy beats nothing. */
async function cacheFirst(event, store, opts) {
  const request = event.request;
  const key = opts.byUrl ? request.url : request;
  const how = { ignoreVary: true, ignoreSearch: !!opts.ignoreSearch };
  let saved = await lookup(store, key, how);
  if (!saved && opts.also) saved = await lookup(opts.also, key, how);
  if (saved && !(opts.maxAge && ageOf(saved) > opts.maxAge)) return saved;
  try {
    const res = await fetchReadable(request);
    if (isOk(res)) event.waitUntil(put(store, key, res.clone()));
    return res;
  } catch {
    return saved || Response.error();
  }
}

/** Pictures and icons of the site itself: the saved copy at once, the network refreshing it. */
async function savedThenRefresh(event, store) {
  const request = event.request;
  const answer = saving(event, fetch(request), store, isOk, request);
  const saved = await lookup(store, request, { ignoreVary: true });
  if (saved) return saved;
  try {
    return await answer;
  } catch {
    return Response.error();
  }
}

/** ?sw=off: drop every saved copy and unregister; this load goes to the network as usual. */
async function switchOff(event) {
  event.waitUntil(turnOff());
  try {
    return await fromNetwork(event);
  } catch {
    return Response.error();
  }
}

async function turnOff() {
  off = true;
  try {
    const names = (await caches.keys()).filter((n) => n.startsWith(PREFIX));
    await Promise.all(names.map((n) => caches.delete(n)));
    await self.registration.unregister();
  } catch {
    // the page deletes the stores and unregisters as well
  }
}

// ---------------------------------------------------------------------------------------------
// the network

/** The navigation preload's answer when there is one, else a fetch. */
async function fromNetwork(event) {
  try {
    const pre = await event.preloadResponse;
    if (pre) return pre;
  } catch {
    // the preload failed (no network): the fetch below fails the same way, or works if it was cancelled
  }
  return fetch(event.request);
}

/** A cross-origin picture asked for without CORS is fetched with CORS, so its status can be read
 *  and a good one saved. A host that refuses CORS gets the original request, which is not saved. */
async function fetchReadable(request) {
  if (request.mode !== 'no-cors' || new URL(request.url).origin === self.location.origin) return fetch(request);
  try {
    return await fetch(new Request(request.url, { mode: 'cors', credentials: 'omit' }));
  } catch {
    return fetch(request);
  }
}

/** Pass a network answer through, saving a copy of a good one. The copy is cloned before anyone
 *  reads the body, and the save runs on after the answer is handed back. */
function saving(event, answer, store, good, key) {
  let copying = null;
  const out = answer.then((res) => {
    if (good(res)) copying = put(store, key, res.clone());
    return res;
  });
  event.waitUntil(out.then(() => copying, () => null));
  return out;
}

/** Settles with {res} (the network answered), {failed} (it threw) or {late} (no answer in `ms`). */
function within(promise, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve({ late: true }), ms);
    promise.then(
      (res) => {
        clearTimeout(timer);
        resolve({ res });
      },
      () => {
        clearTimeout(timer);
        resolve({ failed: true });
      },
    );
  });
}

const isOk = (res) => res.status === 200 && !res.redirected && (res.type === 'basic' || res.type === 'cors' || res.type === 'default');
const contentType = (res) => res.headers.get('content-type') || '';
const isHtml = (res) => isOk(res) && contentType(res).includes('text/html');
const isFlight = (res) => isOk(res) && contentType(res).startsWith('text/x-component');

// ---------------------------------------------------------------------------------------------
// saved copies

/** Save a response with the time it was saved. Never throws: a full disk or a dropped stream only
 *  means this copy is not kept. */
async function put(store, key, res) {
  if (off) return;
  try {
    const cache = await caches.open(store.name);
    await cache.put(key, withHeader(res, STAMP, String(Date.now())));
    if (store.max) await trimNow(cache, store);
  } catch {
    // the answer itself was already handed back; this copy is just not kept
  }
}

/** The same response with one header set (a fetched or saved response's headers are read-only). */
function withHeader(res, name, value) {
  const headers = new Headers(res.headers);
  headers.set(name, value);
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

/** The saved copy of `key` in a store, if there is one. */
const lookup = async (store, key, how) => (await caches.open(store.name)).match(key, how);

const savedAt = (res) => Number(res.headers.get(STAMP)) || null;
const ageOf = (res) => {
  const at = savedAt(res);
  return at ? Date.now() - at : 0;
};

/** The saved page: this exact address, else the same path without its query, else any query. */
async function savedPage(href) {
  const cache = await caches.open(STORE.pages.name);
  const url = new URL(href);
  return (
    (await cache.match(href, { ignoreVary: true })) ||
    (url.search ? await cache.match(url.origin + url.pathname, { ignoreVary: true }) : undefined) ||
    (await cache.match(href, { ignoreVary: true, ignoreSearch: true }))
  );
}

/** A saved copy handed to a page load, marked so the page can say how old it is (Server-Timing is
 *  readable from the page: performance.getEntriesByType('navigation')[0].serverTiming). */
function marked(res, reason) {
  return withHeader(res, 'Server-Timing', `blindspot-saved;desc="${reason}";dur=${savedAt(res) || 0}`);
}

/** The screen for a page this browser never saved. */
async function offlineScreen() {
  const saved = await lookup(STORE.core, OFFLINE_PATH, { ignoreVary: true });
  if (saved) return marked(saved, 'missing');
  return new Response(FALLBACK_HTML, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
}

// The plain page shown only if /offline itself could not be saved (no network since install).
const FALLBACK_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Offline · BlindSpot</title><style>:root{color-scheme:dark;--bg:#0c0a08;--ink:#fff;--muted:#b6b0a7}body{margin:0;padding:48px 16px;background:var(--bg);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:36rem;margin:0 auto}p{color:var(--muted)}a{color:var(--ink);display:inline-block;min-height:44px;line-height:44px}</style></head><body><main><h1>You are offline</h1><p>This browser has no saved copy of this page yet. It opens when the connection returns.</p><a href="">Try again</a></main></body></html>`;

/** Save a page and every /_next/static file its HTML names (stylesheets, scripts, fonts, and the
 *  client chunks listed in its payload). */
async function saveDocument(href, pagesStore, filesStore) {
  if (!isSaveablePage(href)) return false;
  const res = await fetch(href, { credentials: 'same-origin', headers: { Accept: 'text/html' } });
  if (!isHtml(res)) return false;
  const html = await res.clone().text();
  await put(pagesStore, new URL(href, self.location.origin).href, res);
  await runLimited(staticRefs(html).map((u) => () => saveIfMissing(filesStore, u)), 6);
  return true;
}

/** Same-origin, and not a ?sw=on / ?sw=off switch (the plain address is saved instead). */
function isSaveablePage(href) {
  try {
    const url = new URL(href, self.location.origin);
    return url.origin === self.location.origin && !url.searchParams.has('sw');
  } catch {
    return false;
  }
}

/** /_next/static/ addresses named in a page, with or without the /_next prefix (payloads drop it). */
function staticRefs(html) {
  const out = new Set();
  for (const m of html.matchAll(/(?:\/_next\/)?static\/(?:chunks|css|media)\/[^"'\s\\()<>?#]+/g)) {
    out.add(new URL(m[0].startsWith('/_next/') ? m[0] : `/_next/${m[0]}`, self.location.origin).href);
  }
  return [...out];
}

async function saveIfMissing(store, href) {
  const cache = await caches.open(store.name);
  if (await cache.match(href, { ignoreVary: true })) return;
  const cross = new URL(href).origin !== self.location.origin;
  const res = await fetch(href, cross ? { mode: 'cors', credentials: 'omit' } : { credentials: 'same-origin' });
  if (isOk(res)) await put(store, href, res);
}

/** What a page loaded before this worker saw it: saved now, each into the store it would have used. */
async function warm(pageHref, urls) {
  // a page that loaded without a worker (first visit, hard reload) is taken over from here on
  await self.clients.claim();
  const jobs = [];
  if (pageHref && isSaveablePage(pageHref)) jobs.push(() => saveDocument(pageHref, STORE.pages, STORE.static));
  const seen = new Set();
  for (const u of urls.slice(0, 400)) {
    if (typeof u !== 'string' || seen.has(u)) continue;
    seen.add(u);
    let url;
    try {
      url = new URL(u);
    } catch {
      continue;
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') continue;
    const name = storeFor(url);
    if (name) jobs.push(() => saveIfMissing(STORE[name], url.href));
  }
  await runLimited(jobs, 6);
  // /offline missed at install (no network then): save it now
  if (!(await lookup(STORE.core, OFFLINE_PATH))) await saveDocument(OFFLINE_PATH, STORE.core, STORE.core).catch(() => false);
}

async function runLimited(jobs, lanes) {
  let next = 0;
  const lane = async () => {
    while (next < jobs.length) {
      const job = jobs[next++];
      try {
        await job();
      } catch {
        // one missing file does not stop the rest
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(lanes, jobs.length) }, lane));
}

// Trimming reads every key, so it runs on every tenth save to a store (and on activate): a cap can
// be passed by up to nine entries between trims.
const savesSinceTrim = new Map();
async function trimNow(cache, store) {
  const n = (savesSinceTrim.get(store.name) || 0) + 1;
  savesSinceTrim.set(store.name, n % 10);
  if (n % 10 === 0) await trim(cache, store.max);
}

/** Keep the newest `max` entries (keys() lists them oldest written first). */
async function trim(cache, max) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

// ---------------------------------------------------------------------------------------------
// telling the page

// Pages last told they are showing a saved copy, so the first fresh answer after it is always sent.
const stale = new Set();
const liveSentAt = new Map();

function tell(event, msg) {
  const id = event.clientId;
  if (!id) return;
  event.waitUntil(
    self.clients
      .get(id)
      // only windows: a map's web worker asks for tiles too, and must not get these messages
      .then((c) => (c && c.type === 'window' ? c.postMessage({ source: 'blindspot-sw', ...msg }) : null))
      .catch(() => null),
  );
}

function tellSaved(event, kind, reason, saved) {
  if (event.clientId) stale.add(event.clientId);
  tell(event, { type: 'saved', kind, reason, savedAt: savedAt(saved), url: event.request.url });
}

/** A fresh answer reached the page: always after a saved one, otherwise at most every 15 s. */
function tellLive(event, kind) {
  const id = event.clientId;
  if (!id) return;
  const now = Date.now();
  if (!stale.has(id) && now - (liveSentAt.get(id) || 0) < 15000) return;
  stale.delete(id);
  if (liveSentAt.size > 200) liveSentAt.clear();
  liveSentAt.set(id, now);
  tell(event, { type: 'live', kind });
}
