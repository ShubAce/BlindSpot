import Link from 'next/link';

import { s } from './styles';

/**
 * The page's header bar: the h1 (the name with what it does), the one supporting sentence, two links
 * and the one action. On a phone the links and the action fold into a Menu (a <details>, so it opens
 * without script) and the sentence takes a second line under the name.
 *
 * The sentence is the thesis in words, no figure: the figures sit just below it (the share of burnt
 * area with no fire alert, and the clock of who looks when), each read from the API.
 */
export function Rail() {
  return (
    <header className={s.rail}>
      <div className={s.railInner}>
        <h1 className={s.railTitle}>
          <Link href="/" className={s.railHome}>
            BlindSpot looks between the passes
          </Link>
        </h1>
        <p className={s.railSentence}>Fields are lit after the fire satellites pass over, so their fire counts miss most of the burning.</p>
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
