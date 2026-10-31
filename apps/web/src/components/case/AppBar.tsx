import Link from 'next/link';

/**
 * The top bar of the app screens that live outside mission control (the case file and the bulletin):
 * the product's mark and name, then the three other places, the current one marked by the app's one
 * selection mark (a 1px gold line as wide as its label). 48px tall, a 1px rule below, no chrome.
 * A Server Component: it holds no state, so it costs no JavaScript.
 */

export type AppPlace = 'live' | 'case' | 'bulletin' | 'method';

const LINKS: { href: string; label: string; place: AppPlace }[] = [
  { href: '/live', label: 'Live map', place: 'live' },
  { href: '/bulletin', label: 'Bulletin', place: 'bulletin' },
  { href: '/method', label: 'Method', place: 'method' },
];

/** The mark: a ring (a satellite's footprint) with a dark notch where it cannot see. */
export function BlindSpotMark({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="7.5" fill="none" stroke="var(--accent)" strokeWidth="2" />
      <circle cx="13.6" cy="6.4" r="3.6" fill="var(--ground, var(--bg))" />
      <circle cx="10" cy="10" r="2" fill="var(--accent)" />
    </svg>
  );
}

export function AppBar({ current, crumb }: { current: AppPlace; crumb?: string }) {
  return (
    <header className="app-bar" data-print="hide">
      <div className="app-bar__inner">
        <Link href="/" className="app-bar__brand" aria-label="BlindSpot home">
          <BlindSpotMark />
          <span>BlindSpot</span>
        </Link>
        {crumb ? (
          <span className="app-bar__crumb" aria-hidden="true">
            {crumb}
          </span>
        ) : null}
        <nav aria-label="Main" className="app-bar__nav">
          {LINKS.map((l) => (
            <Link key={l.href} href={l.href} className={`app-bar__link${l.place === current ? ' selected-line' : ''}`} aria-current={l.place === current ? 'page' : undefined}>
              {l.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
