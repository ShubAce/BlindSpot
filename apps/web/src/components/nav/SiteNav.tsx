import Link from 'next/link';
import type { ReactNode } from 'react';

import { ThemeToggle } from './ThemeToggle';
import './nav.css';

/**
 * The one navigation of every page: the landing, mission control, case files, field briefs, the
 * bulletin, the method, the lab, the offline screen, 404 and the error page. It is the same bar on
 * all of them, item for item and pixel for pixel; only the current-place mark moves (the 1px gold line
 * and aria-current). A page's own call to action lives on the page (the landing's headline row has
 * "Open mission control"), never in the bar, so the bar never changes shape from one page to the next.
 *
 * Desktop and tablet: a 56px bar pinned to the top, the mark and name (home) on the left, the four
 * places as text links and the theme switch on the right. The current place carries the site's one
 * selection mark, a 1px gold line as wide as its label, sitting on the bar's rule.
 * Phone (700px and under): the top bar keeps the mark and name and the theme switch, and scrolls
 * away; the five places, home included, move to a bar fixed at the bottom (icon over a short label),
 * the current one marked by the same gold line.
 *
 * It reads nothing from the page around it: its colours, type and insets are its own (nav.css), so it
 * is the same bar on the page surface and the app surface. A Server Component that also renders inside
 * client screens (mission control, the error page); the theme switch is its one client part.
 */

export type Place = 'home' | 'live' | 'brief' | 'bulletin' | 'method';

const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' } as const;

// 18px outline icons drawn at 20px, one stroke weight, round ends.
const ICONS: Record<Place, ReactNode> = {
  home: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path {...S} d="M2.75 8.25L9 3l6.25 5.25" />
      <path {...S} d="M4.5 7v8h9V7M7.5 15v-4h3v4" />
    </svg>
  ),
  live: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path {...S} d="M2.5 4.5l4-1.5 5 1.8 4-1.5v10.2l-4 1.5-5-1.8-4 1.5z" />
      <path {...S} d="M6.5 3v10.2M11.5 4.8V15" />
    </svg>
  ),
  // a clipboard with a tick: what a field team carries out to the villages
  brief: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path {...S} d="M6.25 3.25h-2v12.5h9.5V3.25h-2" />
      <path {...S} d="M6.25 2.25h5.5v2.5h-5.5z" />
      <path {...S} d="M6.5 10.25l1.75 1.75 3.25-3.5" />
    </svg>
  ),
  bulletin: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path {...S} d="M4 2.5h7l3 3v10H4z" />
      <path {...S} d="M6.5 8h5M6.5 10.5h5M6.5 13h3" />
    </svg>
  ),
  method: (
    <svg width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path {...S} d="M7 2.5h4M7.75 2.5v4.6L3.6 14a1 1 0 0 0 .86 1.5h9.08a1 1 0 0 0 .86-1.5L10.25 7.1V2.5" />
      <path {...S} d="M5.4 11h7.2" />
    </svg>
  ),
};

// The bar's labels, and the bottom bar's shorter ones (five tabs share 390px).
const PLACES: { place: Place; href: string; label: string; short: string }[] = [
  { place: 'home', href: '/', label: 'Home', short: 'Home' },
  { place: 'live', href: '/live', label: 'Live map', short: 'Live map' },
  { place: 'brief', href: '/brief', label: 'Field brief', short: 'Brief' },
  { place: 'bulletin', href: '/bulletin', label: 'Bulletin', short: 'Bulletin' },
  { place: 'method', href: '/method', label: 'Method', short: 'Method' },
];

/**
 * The mark (the favicon's): a ring, a satellite's footprint, with a notch where it cannot see. The
 * ring and dot are the gold of a mark (--sn-gold, the page's --accent-mark): the brand gold on dark; on
 * light its deepest shade that still reads as gold (3.32:1 on the bone bar, where the brand gold,
 * 2.03:1, washed out beside the ink name). The notch is the bar showing through.
 */
export function BlindSpotMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="7.5" fill="none" stroke="var(--sn-gold)" strokeWidth="2" />
      <circle cx="13.6" cy="6.4" r="3.6" fill="var(--sn-ground)" />
      <circle cx="10" cy="10" r="2" fill="var(--sn-gold)" />
    </svg>
  );
}

/**
 * `current`: the place this page is (or belongs to); none for pages outside the four places (the
 * lab, the offline screen, 404, the error page). `within`: the page sits inside that place without
 * being it (a case file inside the live map), so the mark says "here" (aria-current="true") rather
 * than "this page".
 *
 * Every page's first Tab stop is the bar's skip link, in one look everywhere (nav.css .sn-skip).
 * `skipTo` is where it lands (the page's <main id="main"> by default; mission control's map) and
 * `skipLabel` says what is there. It sits outside the bar's header, so the bar's markup stays the
 * same on every page.
 */
export function SiteNav({
  current,
  within = false,
  skipTo = '#main',
  skipLabel = 'Skip to the content',
}: {
  current?: Place;
  within?: boolean;
  skipTo?: string;
  skipLabel?: string;
}) {
  const mark = (p: Place) => (p === current ? (within ? 'true' : 'page') : undefined);
  // Leaving the landing for mission control plays its one view transition (app/page.tsx).
  const toLive = (p: Place) => (p === 'live' ? ['to-live'] : undefined);
  return (
    <>
      <a href={skipTo} className="sn-skip" data-print="hide">
        {skipLabel}
      </a>
      <header className="sn-top" data-print="hide">
        <div className="sn-top__inner">
          {/* home is the brand on desktop: it carries aria-current there, and no line (its tab does) */}
          <Link href="/" className="sn-brand" aria-label="BlindSpot, home" aria-current={mark('home')}>
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
          <ThemeToggle />
        </div>
      </header>
      <nav aria-label="Main" className="sn-tabs" data-print="hide">
        <ul>
          {PLACES.map((p) => (
            <li key={p.place}>
              <Link href={p.href} className="sn-tab" aria-current={mark(p.place)} transitionTypes={toLive(p.place)}>
                {ICONS[p.place]}
                <span>{p.short}</span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </>
  );
}
