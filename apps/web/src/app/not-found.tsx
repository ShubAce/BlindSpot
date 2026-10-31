import type { Metadata } from 'next';
import Link from 'next/link';

import { SiteNav } from '@/components/nav/SiteNav';

// Next adds noindex to a 404 itself.
export const metadata: Metadata = { title: 'Page not found' };

/**
 * Any address the site does not have. Plain words and the three ways in; no figure, so it never
 * needs the API. (A case number the API does not know has its own page under /case.)
 */
export default function NotFound() {
  return (
    <>
      <SiteNav />
      <main id="main" className="mx-auto flex min-h-[calc(100svh-var(--sn-top-h)-var(--sn-tabs-h))] max-w-[72rem] flex-col justify-center px-4 py-16 sm:px-8">
        <h1 className="font-display text-head leading-[0.9] uppercase">Nothing here</h1>
        <p className="mt-6 max-w-[36rem] text-lead text-ink">
          BlindSpot has no page at this address. The live map, the daily bulletin and the method are where everything lives.
        </p>
        <p className="mt-10 flex flex-wrap items-center gap-x-8 gap-y-4">
          <Link href="/live" className="inline-flex min-h-12 items-center bg-accent px-5 font-semibold text-accent-ink">
            Open mission control
          </Link>
          <Link href="/" className="inline-flex min-h-12 items-center underline underline-offset-4">
            Home
          </Link>
          <Link href="/bulletin" className="inline-flex min-h-12 items-center underline underline-offset-4">
            Bulletin
          </Link>
          <Link href="/method" className="inline-flex min-h-12 items-center underline underline-offset-4">
            Method
          </Link>
        </p>
      </main>
    </>
  );
}
