'use client';

import { useEffect, useState, useSyncExternalStore } from 'react';

import { CACHE_PREFIX, markScreen, SAVED_AT_HEADER, siteAnswers } from './connectivity';
import { savedAgo, savedStamp, useClock } from './time';

/**
 * The /offline screen. The service worker shows it in place of a page this browser never saved,
 * when there is no network to fetch it (the address stays the one asked for). It says so, lists the
 * pages this browser has saved with when each was saved, and loads the page asked for by itself once
 * the site answers again. Opened directly at /offline while online, it explains what it is for.
 *
 * Everything here is read in the browser (the address, the saved pages), so the page itself is
 * static and the worker saves it at install.
 */

type SavedPage = { href: string; title: string; savedAt: number | null };

const still = () => () => undefined;
function subscribeOnline(cb: () => void) {
  window.addEventListener('online', cb);
  window.addEventListener('offline', cb);
  return () => {
    window.removeEventListener('online', cb);
    window.removeEventListener('offline', cb);
  };
}

export function OfflineScreen() {
  // The server render (and the first paint of the saved copy) assumes the usual case: offline, standing
  // in for a page; opened at /offline directly with a network, it explains itself instead.
  const online = useSyncExternalStore(subscribeOnline, () => navigator.onLine, () => false);
  const asked = useSyncExternalStore(
    still,
    () => (window.location.pathname === '/offline' ? null : `${window.location.pathname}${window.location.search}`),
    () => null,
  );
  const explaining = online && asked == null;
  const [pages, setPages] = useState<SavedPage[] | null>(null);
  const now = useClock(!!pages?.length);

  // the screen explains the situation itself, so the site-wide notice stays out of the way
  useEffect(() => {
    markScreen(true);
    return () => markScreen(false);
  }, []);

  useEffect(() => {
    let live = true;
    void listSavedPages().then((p) => {
      if (live) setPages(p);
    });
    return () => {
      live = false;
    };
  }, []);

  // Standing in for another page: load it as soon as the site answers. The online event alone is not
  // enough (venue Wi-Fi without internet never fires it), so the site is also asked every 15 s.
  useEffect(() => {
    if (!asked) return;
    let stopped = false;
    const check = async () => {
      // with no network at all the browser says so: wait for its online event instead of asking
      if (stopped || document.visibilityState !== 'visible' || !navigator.onLine) return;
      if ((await siteAnswers()) && !stopped) window.location.reload();
    };
    const timer = window.setInterval(check, 15_000);
    window.addEventListener('online', check);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.removeEventListener('online', check);
    };
  }, [asked]);

  const lead = explaining
    ? 'BlindSpot saves every page opened in this browser with the last data it showed, so the page still opens without a connection. A page never opened here shows this screen instead.'
    : `This browser has no saved copy of ${asked ?? 'this page'} yet. Pages opened while online are saved with the last data they showed, and those listed below still open.${
        asked ? ' This one loads by itself once the connection is back.' : ''
      }`;

  return (
    <>
      <div className="title-row">
        <div className="title-row__text">
          <h1>{explaining ? 'Pages saved for offline use' : 'You are offline'}</h1>
          <p className="finding">{lead}</p>
        </div>
        {!explaining ? (
          <div className="title-row__actions">
            <button type="button" className="button-primary pwa-retry" onClick={() => window.location.reload()}>
              Try again
            </button>
          </div>
        ) : null}
      </div>

      <section className="card pwa-saved" aria-labelledby="pwa-saved-title">
        <div className="card__head">
          <h2 className="card__title" id="pwa-saved-title">
            Saved in this browser
          </h2>
          {pages ? <span className="card__aside">{pages.length === 1 ? '1 page' : `${pages.length} pages`}</span> : null}
        </div>
        {pages === null ? (
          <ul className="prov" aria-hidden="true">
            {[0, 1, 2].map((i) => (
              <li key={i} className="prov__item">
                <span className="skeleton" style={{ width: '40%', height: 20 }} />
                <span className="skeleton" style={{ width: '25%', height: 16 }} />
              </li>
            ))}
          </ul>
        ) : pages.length === 0 ? (
          <p className="card__note pwa-saved__empty">Nothing is saved yet. Pages are saved as they are opened while online.</p>
        ) : (
          <ul className="prov">
            {pages.map((p) => (
              <li key={p.href} className="prov__item">
                {/* a full page load, which the worker answers from its saved copy */}
                <a className="pwa-saved__link" href={p.href}>
                  {p.title}
                </a>
                <span className="prov__meta nums">
                  {p.href}
                  {p.savedAt && now ? ` · saved ${savedStamp(p.savedAt, now)}, ${savedAgo(p.savedAt, now)}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="card__note">Each opens with the figures it showed when it was saved, and a notice on the page says how old they are.</p>
      </section>
    </>
  );
}

/** The pages the worker has saved, newest first, one per address (read from the Cache Storage it
 *  writes: blindspot-pages-<version>), all read at once. */
async function listSavedPages(): Promise<SavedPage[]> {
  if (!('caches' in window)) return [];
  try {
    const names = (await caches.keys()).filter((n) => n.startsWith(`${CACHE_PREFIX}pages-`));
    const found = await Promise.all(
      names.map(async (name) => {
        const cache = await caches.open(name);
        const keys = (await cache.keys()).slice(-60).filter((req) => {
          const url = new URL(req.url);
          return url.origin === window.location.origin && url.pathname !== '/offline' && !url.searchParams.has('sw');
        });
        return Promise.all(
          keys.map(async (req): Promise<SavedPage | null> => {
            const res = await cache.match(req);
            if (!res) return null;
            const url = new URL(req.url);
            const href = `${url.pathname}${url.search}`;
            return { href, savedAt: Number(res.headers.get(SAVED_AT_HEADER)) || null, title: (await titleOf(res)) ?? href };
          }),
        );
      }),
    );
    const byHref = new Map<string, SavedPage>();
    for (const p of found.flat()) {
      const prev = p && byHref.get(p.href);
      if (p && !(prev && (prev.savedAt ?? 0) >= (p.savedAt ?? 0))) byHref.set(p.href, p);
    }
    return [...byHref.values()].sort((a, b) => (b.savedAt ?? 0) - (a.savedAt ?? 0));
  } catch {
    return [];
  }
}

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", '#x27': "'", nbsp: ' ' };
const TITLE = /<title[^>]*>([^<]{1,300})<\/title>/i;

/** The page's own <title>, without the site's suffix. Only the start of the page is read: the title
 *  sits in its first 2 KB, or about 48 KB in where the page streams it (a case file), while a whole
 *  page is 100 to 500 KB. */
async function titleOf(res: Response): Promise<string | null> {
  const reader = res.body?.getReader();
  if (!reader) return null;
  const decoder = new TextDecoder();
  let html = '';
  try {
    while (html.length < 200_000) {
      const { done, value } = await reader.read();
      if (done) break;
      html += decoder.decode(value, { stream: true });
      const m = TITLE.exec(html);
      if (m) {
        const t = m[1]
          .replace(/&(amp|lt|gt|quot|#39|#x27|nbsp);/g, (all, e: string) => ENTITIES[e] ?? all)
          .replace(/\s*·\s*BlindSpot\s*$/, '')
          .trim();
        return t || null;
      }
    }
    return null;
  } catch {
    return null;
  } finally {
    void reader.cancel().catch(() => undefined);
  }
}
