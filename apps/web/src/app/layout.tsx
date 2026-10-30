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

export const metadata: Metadata = {
  title: { default: 'BlindSpot', template: '%s · BlindSpot' },
  description:
    'Satellites look at 1:30. Fires start at 5. BlindSpot maps the Punjab crop fires that fire satellites miss, from the burn scars Sentinel-2 photographs, and traces their smoke to Delhi.',
  applicationName: 'BlindSpot',
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
