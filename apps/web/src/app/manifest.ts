import type { MetadataRoute } from 'next';

/**
 * The web app manifest (served at /manifest.webmanifest, linked by Next from every page): makes
 * BlindSpot installable, with the service worker (public/sw.js) keeping the pages it has seen
 * working offline. Colours are the dark ground #0c0a08 (the char of a burnt field), so the window
 * and the splash screen open on the site's own colour. Icons are drawn from app/icon.svg into
 * public/icons/: the maskable one keeps the mark inside the central safe circle.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    id: '/',
    name: 'BlindSpot',
    short_name: 'BlindSpot',
    description:
      'BlindSpot maps the Sentinel-2 burn scars in Punjab and Haryana that no fire alert matched, clocks the evening burning the polar fire satellites do not report, and traces the smoke to Delhi.',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    background_color: '#0c0a08',
    theme_color: '#0c0a08',
    lang: 'en-IN',
    dir: 'ltr',
    icons: [
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
    shortcuts: [
      { name: 'Mission control', short_name: 'Live map', url: '/live', icons: [{ src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png' }] },
      { name: 'Daily bulletin', short_name: 'Bulletin', url: '/bulletin', icons: [{ src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png' }] },
    ],
  };
}
