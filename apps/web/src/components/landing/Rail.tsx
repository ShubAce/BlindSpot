'use client';

import Link from 'next/link';
import { useEffect, useRef, useState, type ReactNode } from 'react';

import { s } from './styles';
import { PassLine } from './PassLine';

/**
 * The hero's rail (direction sheet, "Navigation on the midline"): one bar across the exact vertical
 * centre of the first screen holding the h1, the sentence with the live pass line, two links and the
 * one action. It is sticky in the page's flow, so it rises with the pictures and docks as the header
 * bar, taking the page ground once the pictures have passed (`data-docked`).
 *
 * On a phone the rail keeps the h1 and a Menu; the sentence, the pass line and the action sit in the
 * first screen's bottom-left corner instead (HeroFoot).
 *
 * `sentence` is the server's RailSentence (the gap between the satellites' looks, read from the API)
 * inside its own <Suspense>, so the rail paints at once and its figure streams in.
 */
export function Rail({ sentence }: { sentence: ReactNode }) {
  const ref = useRef<HTMLElement | null>(null);
  const [docked, setDocked] = useState(false);

  useEffect(() => {
    const end = document.getElementById('hero-end');
    const rail = ref.current;
    if (!end || !rail) return;
    const update = () => setDocked(end.getBoundingClientRect().top <= rail.offsetHeight + 1);
    update();
    window.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  }, []);

  return (
    <header ref={ref} className={s.rail} data-docked={docked ? 'yes' : 'no'}>
      <div className={s.railInner}>
        <h1 className={s.railTitle}>
          <Link href="/" className={s.railHome}>
            BlindSpot counts unseen fires
          </Link>
        </h1>
        <div className={s.railSay}>
          <p className={s.railSentence}>{sentence}</p>
          <PassLine className={s.railPass} />
        </div>
        <nav className={s.railNav} aria-label="Main">
          <Link href="/method" className={s.railLink}>
            Method
          </Link>
          <Link href="/bulletin" className={s.railLink}>
            Bulletin
          </Link>
          <Link href="/live" className={s.button} transitionTypes={['to-live']}>
            Open mission control
          </Link>
        </nav>
        <details className={s.menu}>
          <summary className={s.menuButton}>Menu</summary>
          <nav className={s.menuPanel} aria-label="Main">
            <Link href="/live" transitionTypes={['to-live']}>
              Open mission control
            </Link>
            <Link href="/method">Method</Link>
            <Link href="/bulletin">Bulletin</Link>
          </nav>
        </details>
      </div>
    </header>
  );
}

/** The phone's first-screen corner: the sentence, the live pass line and the action as a full-width
 *  button, over a scrim limited to that corner. Hidden on wider screens, where the rail holds them. */
export function HeroFoot({ sentence }: { sentence: ReactNode }) {
  return (
    <div className={s.heroFoot}>
      <p className={s.footSentence}>{sentence}</p>
      <PassLine className={s.footPass} />
      <Link href="/live" className={`${s.button} ${s.buttonWide}`} transitionTypes={['to-live']}>
        Open mission control
      </Link>
    </div>
  );
}
