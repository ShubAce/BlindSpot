import Link from 'next/link';
import type { ReactNode } from 'react';

import styles from './styles';

/**
 * The icon rail (data-terminal shell): BlindSpot's mark at the top, the screens as 16px outline
 * icons, Home pushed to the bottom. Each icon has its name as a tooltip and as its accessible name.
 * On a phone it becomes a bottom bar with short labels. The active screen is marked by the one
 * gold line on the rail's edge.
 */

type Item = { href: string; label: string; icon: ReactNode };

const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' } as const;

const ICONS = {
  overview: (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M2.5 4.5l4-1.5 5 1.8 4-1.5v10.2l-4 1.5-5-1.8-4 1.5z" />
      <path {...S} d="M6.5 3v10.2M11.5 4.8V15" />
    </svg>
  ),
  bulletin: (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M4 2.5h7l3 3v10H4z" />
      <path {...S} d="M6.5 8h5M6.5 10.5h5M6.5 13h3" />
    </svg>
  ),
  method: (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M7 2.5h4M7.75 2.5v4.6L3.6 14a1 1 0 0 0 .86 1.5h9.08a1 1 0 0 0 .86-1.5L10.25 7.1V2.5" />
      <path {...S} d="M5.4 11h7.2" />
    </svg>
  ),
  home: (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path {...S} d="M2.75 8.25L9 3l6.25 5.25" />
      <path {...S} d="M4.5 7v8h9V7M7.5 15v-4h3v4" />
    </svg>
  ),
};

const NAV: Item[] = [
  { href: '/live', label: 'Overview', icon: ICONS.overview },
  { href: '/bulletin', label: 'Bulletin', icon: ICONS.bulletin },
  { href: '/method', label: 'Method', icon: ICONS.method },
];
const HOME: Item = { href: '/', label: 'Home', icon: ICONS.home };

/** BlindSpot's mark: the ring a satellite sweeps, broken where it does not look, and the field it misses. */
export function BlindSpotMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" aria-hidden="true">
      <path d="M17.6 6.2A8 8 0 1 0 19 11" fill="none" stroke="var(--accent)" strokeWidth="1.75" strokeLinecap="round" />
      <rect x="15.5" y="7.5" width="4.5" height="4.5" fill="var(--accent)" />
    </svg>
  );
}

function RailLink({ item, active }: { item: Item; active: boolean }) {
  return (
    <li>
      <Link href={item.href} className={styles.railLink} aria-current={active ? 'page' : undefined} aria-label={item.label} data-tip={item.label}>
        {item.icon}
        <span className={styles.railText}>{item.label}</span>
      </Link>
    </li>
  );
}

export function IconRail({ active }: { active: string }) {
  return (
    <nav className={styles.rail} aria-label="BlindSpot">
      <Link href="/" className={styles.railMark} aria-label="BlindSpot, home">
        <BlindSpotMark />
      </Link>
      <ul className={styles.railList}>
        {NAV.map((it) => (
          <RailLink key={it.href} item={it} active={active === it.href} />
        ))}
      </ul>
      <ul className={`${styles.railList} ${styles.railBottom}`}>
        <RailLink item={HOME} active={active === HOME.href} />
      </ul>
    </nav>
  );
}
