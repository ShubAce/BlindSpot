import Script from 'next/script';

/**
 * Google Analytics 4: page views, sources (search, links, social), countries and devices, visible
 * only to the Google account that owns the property. Off unless NEXT_PUBLIC_GA_ID (a measurement ID,
 * G-XXXXXXXXXX) is set at build time (.env at the repo root -> infra/aws/deploy.py -> web.env).
 * Load the property one way only: its Tag Manager container fires the same GA4 tag, so adding the
 * container as well would count every page view twice.
 *
 * gtag.js loads after the page is interactive, so it never delays the first paint. Client-side
 * navigations are counted by GA4's enhanced measurement (browser history changes), on by default for
 * a web stream. The service worker (public/sw.js) handles only this site's own files, the API, the
 * basemap and NASA GIBS, so analytics requests always go straight to the network.
 */
const GA_ID = process.env.NEXT_PUBLIC_GA_ID?.trim();

export function GoogleAnalytics() {
  if (!GA_ID || !/^(G|GT)-[A-Z0-9]{4,16}$/.test(GA_ID)) return null;
  return (
    <>
      <Script src={`https://www.googletagmanager.com/gtag/js?id=${GA_ID}`} strategy="afterInteractive" />
      <Script id="ga4" strategy="afterInteractive">
        {`window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','${GA_ID}');`}
      </Script>
    </>
  );
}
