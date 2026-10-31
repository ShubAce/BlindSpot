import Link from 'next/link';
import type { ReactNode } from 'react';

import './nav.css';

/**
 * The one navigation of every page (landing, mission control, case files, bulletin, method, 404).
 *
 * Desktop and tablet: a 56px bar pinned to the top: the mark and name (home) on the left, the three
 * places as text links and the one action on the right; the current place carries the site's one
 * selection mark, a 1px gold line as wide as its label, sitting on the bar's rule.
 * Phone (700px and under): the bar keeps only the mark and name and scrolls away; the four places
 * move to a bar fixed at the bottom with short labels, the current one marked by the same gold line.
 *
 * Its colours are its own, not the surface's, so it is the same bar on the page and the app surface.
 * A page aligns it with its content through --sn-max and --sn-pad. A Server Component: no state.
 */

export type Place = 'home' | 'live' | 'bulletin' | 'method';

const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' } as const;

const ICONS: Record<Place, ReactNode> = {
  home: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M2.75 8.25L9 3l6.25 5.25" />
      <path {...S} d="M4.5 7v8h9V7M7.5 15v-4h3v4" />
    </svg>
  ),
  live: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M2.5 4.5l4-1.5 5 1.8 4-1.5v10.2l-4 1.5-5-1.8-4 1.5z" />
      <path {...S} d="M6.5 3v10.2M11.5 4.8V15" />
    </svg>
  ),
  bulletin: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M4 2.5h7l3 3v10H4z" />
      <path {...S} d="M6.5 8h5M6.5 10.5h5M6.5 13h3" />
    </svg>
  ),
  method: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M7 2.5h4M7.75 2.5v4.6L3.6 14a1 1 0 0 0 .86 1.5h9.08a1 1 0 0 0 .86-1.5L10.25 7.1V2.5" />
      <path {...S} d="M5.4 11h7.2" />
    </svg>
  ),
};

const PLACES: { place: Place; href: string; label: string }[] = [
  { place: 'home', href: '/', label: 'Home' },
  { place: 'live', href: '/live', label: 'Live map' },
  { place: 'bulletin', href: '/bulletin', label: 'Bulletin' },
  { place: 'method', href: '/method', label: 'Method' },
];

/** The mark (the favicon's): a ring, a satellite's footprint, with a dark notch where it cannot see. */
export function BlindSpotMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="7.5" fill="none" stroke="var(--accent)" strokeWidth="2" />
      <circle cx="13.6" cy="6.4" r="3.6" fill="#0c0a08" />
      <circle cx="10" cy="10" r="2" fill="var(--accent)" />
    </svg>
  );
}

/**
 * `current`: the place this page is (or belongs to). `within`: the page sits inside that place
 * without being it (a case file inside the live map), so the mark says "here" without "this page".
 */
export function SiteNav({ current, within = false }: { current?: Place; within?: boolean }) {
  const mark = (p: Place) => (p === current ? (within ? 'true' : 'page') : undefined);
  // Leaving the landing for mission control plays its one view transition (app/page.tsx).
  const toLive = (p: Place) => (p === 'live' ? ['to-live'] : undefined);
  return (
    <>
      <header className="sn-top" data-print="hide">
        <div className="sn-top__inner">
          <Link href="/" className="sn-brand" aria-label="BlindSpot, home">
            <BlindSpotMark />
            <span>BlindSpot</span>
          </Link>
          <nav aria-label="Main" className="sn-links">
            {PLACES.filter((p) => p.place !== 'home').map((p) => (
              <Link key={p.place} href={p.href} className="sn-link" aria-current={mark(p.place)} transitionTypes={toLive(p.place)}>
                {p.label}
              </Link>
            ))}
          </nav>
          {current === 'live' && !within ? null : (
            <Link href="/live" className="sn-action" transitionTypes={['to-live']}>
              Open mission control
            </Link>
          )}
        </div>
      </header>
      <nav aria-label="Main" className="sn-tabs" data-print="hide">
        <ul>
          {PLACES.map((p) => (
            <li key={p.place}>
              <Link href={p.href} className="sn-tab" aria-current={mark(p.place)} transitionTypes={toLive(p.place)}>
                {ICONS[p.place]}
                <span>{p.label}</span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </>
  );
}
