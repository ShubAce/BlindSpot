import Link from 'next/link';

import { BlindSpotMark } from '@/components/case/AppBar';

/**
 * The page bar for the read-and-scroll pages (method): the wordmark, three plain links and the one
 * action, on the page ground. No chrome, no blur.
 */
export function SiteHeader({ current }: { current: 'method' }) {
  const links = [
    { href: '/live', label: 'Live map', key: 'live' },
    { href: '/bulletin', label: 'Bulletin', key: 'bulletin' },
    { href: '/method', label: 'Method', key: 'method' },
  ];
  return (
    <header className="m-bar">
      <Link href="/" className="m-bar__brand" aria-label="BlindSpot home">
        <BlindSpotMark size={22} />
        <span>BlindSpot</span>
      </Link>
      <nav aria-label="Main" className="m-bar__nav">
        {links.map((l) => (
          <Link key={l.key} href={l.href} className="m-bar__link" aria-current={l.key === current ? 'page' : undefined}>
            {l.label}
          </Link>
        ))}
      </nav>
      <Link href="/live" className="m-bar__action">
        Open mission control
      </Link>
    </header>
  );
}
