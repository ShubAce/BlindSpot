import type { ReactNode } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';

import '@/components/case/app-screen.css';
import '@/components/brief/brief.css';

/** The field brief's frame: the app surface, the site's bar (Field brief marked, on the index and on
 *  every district's brief), and the canvas that the index, a district's brief (or its not-found
 *  page) fill. */
export default function BriefLayout({ children }: { children: ReactNode }) {
  return (
    <div data-surface="app" className="app-screen br-screen">
      <SiteNav current="brief" skipLabel="Skip to the field brief" />
      <main id="main" className="app-canvas br-canvas">
        {children}
      </main>
    </div>
  );
}
