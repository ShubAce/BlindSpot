import type { ReactNode } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';

import '@/components/case/app-screen.css';
import '@/components/brief/brief.css';

/** The field brief's frame: the app surface, the site's bar, and the canvas that the index, a
 *  district's brief (or its not-found page) fill. The bar names no place yet: the brief gets its
 *  entry in the site nav in a later step. */
export default function BriefLayout({ children }: { children: ReactNode }) {
  return (
    <div data-surface="app" className="app-screen br-screen">
      <a className="skip-link" href="#main">
        Skip to the field brief
      </a>
      <SiteNav />
      <main id="main" className="app-canvas br-canvas">
        {children}
      </main>
    </div>
  );
}
