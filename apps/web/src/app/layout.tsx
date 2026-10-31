import type { Metadata, Viewport } from 'next';
import { Anton, Work_Sans } from 'next/font/google';
import type { ReactNode } from 'react';

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
  'BlindSpot maps the Punjab and Haryana crop fires that fire satellites miss, from the burn scars Sentinel-2 photographs, and traces their smoke to Delhi.';

export const metadata: Metadata = {
  ...(SITE_URL ? { metadataBase: new URL(SITE_URL) } : {}),
  title: { default: 'BlindSpot', template: '%s · BlindSpot' },
  description: DESCRIPTION,
  applicationName: 'BlindSpot',
  openGraph: { type: 'website', siteName: 'BlindSpot', locale: 'en_IN' },
  twitter: { card: 'summary_large_image' },
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: '#0c0a08',
  colorScheme: 'dark',
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en" className={`${anton.variable} ${workSans.variable}`}>
      <body>{children}</body>
    </html>
  );
}
