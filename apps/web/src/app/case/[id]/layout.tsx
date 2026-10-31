import type { ReactNode } from 'react';

import { AppBar } from '@/components/case/AppBar';

import '@/components/case/app-screen.css';

/** The case screen's frame: the app surface, the bar, and the canvas the page (or not-found) fills. */
export default function CaseLayout({ children }: { children: ReactNode }) {
  return (
    <div data-surface="app" className="app-screen">
      <a className="skip-link" href="#main">
        Skip to the case file
      </a>
      <AppBar current="case" />
      <main id="main" className="app-canvas">
        {children}
      </main>
    </div>
  );
}
