import type { ReactNode } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';

import '@/components/case/app-screen.css';

/** The case screen's frame: the app surface, the site's bar (a case file belongs to the live map),
 *  and the canvas the page (or not-found) fills. */
export default function CaseLayout({ children }: { children: ReactNode }) {
  return (
    <div data-surface="app" className="app-screen">
      <a className="skip-link" href="#main">
        Skip to the case file
      </a>
      <SiteNav current="live" within />
      <main id="main" className="app-canvas">
        {children}
      </main>
    </div>
  );
}
