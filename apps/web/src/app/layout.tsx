import type { Metadata, Viewport } from 'next';
import { Anton, Work_Sans } from 'next/font/google';
import type { ReactNode } from 'react';

import { OfflineSupport } from '@/components/pwa/OfflineSupport';
import { ThemeScript } from '@/lib/theme';

import './globals.css';

// Anton: one heavy condensed cut for the display moments. Work Sans: the text face, also used for
// every figure (its `tnum` feature is real; see globals.css). Self-hosted by next/font, so the
// browser never calls Google.
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

// Shared links need an absolute base for the share card. On Vercel Next falls back to the production
// (or preview) URL by itself; elsewhere set NEXT_PUBLIC_SITE_URL before `next build`.
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL?.replace(/\/+$/, '');

const DESCRIPTION =
  'BlindSpot maps the Sentinel-2 burn scars in Punjab and Haryana that no fire alert matched, clocks the evening burning the polar fire satellites do not report, and traces the smoke to Delhi.';

export const metadata: Metadata = {
  ...(SITE_URL ? { metadataBase: new URL(SITE_URL) } : {}),
  title: { default: 'BlindSpot', template: '%s · BlindSpot' },
  description: DESCRIPTION,
  applicationName: 'BlindSpot',
  openGraph: { type: 'website', siteName: 'BlindSpot', locale: 'en_IN' },
  twitter: { card: 'summary_large_image' },
  robots: { index: true, follow: true },
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
      </head>
      <body>
        {children}
        <OfflineSupport />
      </body>
    </html>
  );
}
