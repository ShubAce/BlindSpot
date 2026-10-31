'use client';

import Link from 'next/link';
import { useEffect } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';

/**
 * The last net under every page: a part of the screen failed while drawing (not an API answer,
 * which each panel reports itself). Says so plainly and offers to draw it again.
 */
export default function PageError({ error, retry }: { error: unknown; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <>
      <SiteNav />
      <main id="main" className="mx-auto flex min-h-[calc(100svh-var(--sn-top-h)-var(--sn-tabs-h))] max-w-[72rem] flex-col justify-center px-4 py-16 sm:px-8">
        <h1 className="font-display text-head leading-[0.9] uppercase">This page stopped</h1>
        <p className="mt-6 max-w-[36rem] text-lead text-ink">
          Part of this page failed while it was being drawn. Try again; the other pages still work if it keeps failing.
        </p>
        <p className="mt-10 flex flex-wrap items-center gap-x-8 gap-y-4">
          <button type="button" onClick={() => retry()} className="inline-flex min-h-12 cursor-pointer items-center bg-accent px-5 font-semibold text-accent-ink">
            Try again
          </button>
          <Link href="/" className="inline-flex min-h-12 items-center underline underline-offset-4">
            Home
          </Link>
        </p>
      </main>
    </>
  );
}
