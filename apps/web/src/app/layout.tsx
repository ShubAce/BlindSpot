import type { Metadata, Viewport } from 'next';
import { Anton, Work_Sans } from 'next/font/google';
import type { ReactNode } from 'react';
import { GoogleAnalytics } from '@/components/GoogleAnalytics';
import { LoadRecovery } from '@/components/pwa/LoadRecovery';
import { OfflineSupport } from '@/components/pwa/OfflineSupport';
import { KEYWORDS, OPEN_GRAPH, SITE_DESCRIPTION, SITE_GRAPH, SITE_NAME, SITE_URL, jsonLd } from '@/lib/site';
import { ThemeScript } from '@/lib/theme';

import './globals.css';

// Anton: one heavy condensed cut for the display moments. Work Sans: the text face, also used for
// every figure (its `tnum` feature is real; see globals.css). Self-hosted by next/font, so the
// browser never calls Google. Anton's stand-in while it loads is Impact, scaled to Anton's size, so
// the titles keep their lines (the "Anton Fallback" face in globals.css, which must stay imported
// after this font).
const anton = Anton({
  weight: '400',
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-anton',
});

const workSans = Work_Sans({
  weight: ['400', '500', '600'],
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-work-sans',
});

// Search-engine ownership checks (Google Search Console, Bing Webmaster Tools), set at build time when
// the site is verified by meta tag rather than by a DNS record.
const GOOGLE_VERIFICATION = process.env.NEXT_PUBLIC_GOOGLE_SITE_VERIFICATION?.trim();
const BING_VERIFICATION = process.env.NEXT_PUBLIC_BING_SITE_VERIFICATION?.trim();

// Absolute URLs (canonical, share card, sitemap) come from SITE_URL (lib/site.ts). Every indexable page
// sets its own canonical path, so ?days= / ?lang= variants never compete with it in search results.
export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: 'BlindSpot: stubble burning in Punjab the satellites miss', template: '%s · BlindSpot' },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  keywords: KEYWORDS,
  authors: [{ name: 'Team Praise' }],
  creator: 'Team Praise',
  publisher: SITE_NAME,
  category: 'environment',
  // no canonical here: a layout-level one would tell search engines that every page without its own
  // is a copy of the home page. Each indexable page sets `alternates.canonical` itself.
  openGraph: OPEN_GRAPH,
  twitter: { card: 'summary_large_image' },
  robots: { index: true, follow: true, googleBot: { index: true, follow: true, 'max-image-preview': 'large', 'max-snippet': -1 } },
  // unset values render no tag
  verification: {
    google: GOOGLE_VERIFICATION,
    other: BING_VERIFICATION ? { 'msvalidate.01': BING_VERIFICATION } : undefined,
  },
};

// The dark default, which is also what a visitor without JavaScript gets. The light theme overrides
// both in the browser (lib/theme.ts): the colour scheme on <html>, the toolbar colour with its own
// theme-color meta ahead of this one, since two media-less values cannot express a choice.
export const viewport: Viewport = {
  themeColor: '#0c0a08',
  colorScheme: 'dark',
};

// ThemeScript sets <html data-theme> before the first paint (the saved or asked-for theme), so the
// light theme never flashes dark. It changes <html> after the server rendered it, hence
// suppressHydrationWarning there (it covers <html>'s own attributes only). The layout itself reads
// nothing from the request, so every page stays static.
export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en" className={`${anton.variable} ${workSans.variable}`} suppressHydrationWarning>
      <head>
        <ThemeScript />
        {/* who publishes the site, and the site itself (schema.org, lib/site.ts) */}
        <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLd(SITE_GRAPH) }} />
      </head>
      <body>
        {children}
        <OfflineSupport />
        {/* a section the server could not fill (a cold API) asks again by itself: LoadRecovery.tsx */}
        <LoadRecovery />
        {/* Google Analytics 4, only when NEXT_PUBLIC_GA_ID is set (components/GoogleAnalytics.tsx) */}
        <GoogleAnalytics />
      </body>
    </html>
  );
}
