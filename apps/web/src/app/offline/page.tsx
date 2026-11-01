import type { Metadata } from 'next';

import { SiteNav } from '@/components/nav/SiteNav';
import { OfflineScreen } from '@/components/pwa/OfflineScreen';

import '@/components/case/app-screen.css';

export const metadata: Metadata = {
  title: 'Offline',
  description: 'What BlindSpot shows, without a connection, for a page this browser has not saved yet.',
  robots: { index: false, follow: false },
};

/**
 * /offline: the screen the service worker (public/sw.js) shows in place of a page this browser never
 * saved, when there is no network to fetch it. Static on purpose: no figure, no API read, so the
 * worker can save it at install and it always opens. What it lists is read in the browser.
 */
export default function OfflinePage() {
  return (
    <div data-surface="app" className="app-screen">
      <SiteNav skipLabel="Skip to the saved pages" />
      <main id="main" className="app-canvas pwa-canvas">
        <OfflineScreen />
      </main>
    </div>
  );
}
