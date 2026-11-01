'use client';

import { useEffect, type ReactNode } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';

import { AirReceptors } from './AirReceptors';
import { InvestigatorSlot } from './InvestigatorSlot';
import { LiveProvider } from './live-state';
import styles from './styles';
import { MapPanel } from './MapPanel';
import { NextLook } from './NextLook';
import { PassStripPanel } from './PassStripPanel';
import { SourcesStatus } from './SourcesStatus';
import { UnseenList } from './UnseenList';

/**
 * While mission control is on screen, the page says so on <html> (data-lv-sheet): on a phone the
 * ranked list's handle sits fixed above the site's tab bar, and live.css widens the page's
 * scroll-padding by its height so whatever Tab or scrollIntoView brings up from below stops above
 * both. An effect, not a :has() rule, because /live stays mounted (hidden) inside <Activity> after
 * the reader moves on, and its effects' cleanups run then: no other page inherits the padding.
 */
function useSheetPadding() {
  useEffect(() => {
    const html = document.documentElement;
    html.setAttribute('data-lv-sheet', '');
    return () => html.removeAttribute('data-lv-sheet');
  }, []);
}

/**
 * Mission control. The question it answers first: where did fields burn unseen since the last pass,
 * and where is that smoke going?
 *
 * Grid (16px gutters, under the site's 56px top bar), live.css:
 *   1700px and wider: [main 1fr] [side 360px] ([console, its own width, once it renders]); rows
 *     [title auto] [map 1fr] [strip auto], the side column spans map and strip.
 *   1101-1699px (laptops and projectors): [main 1fr] [console]. The title and the map share one
 *     screen's height (`first`), so the map takes the whole width left of the console; the pass strip
 *     and then the side panels, two up, follow under it.
 * Below 1100px one column: title, map, strip, then the side panels two up; below 700px the site's
 * bottom bar takes the navigation and the ranked list is a sheet above it.
 */
export function LiveScreen({ finding }: { finding: ReactNode }) {
  useSheetPadding();
  return (
    <LiveProvider>
      <div data-surface="app" className={styles.shell}>
        <SiteNav current="live" skipTo="#live-map" skipLabel="Skip to the map" />
        <main className={styles.screen} id="main">
          {/* the title and the map: one screen's height together on a laptop, a plain pass-through
              to the grid elsewhere (display: contents) */}
          <div className={styles.first}>
            <header className={styles.titleRow}>
              <div className={styles.titleText}>
                <h1 className={styles.title}>
                  Mission control <span className={styles.scope}>Punjab, Haryana and Delhi</span>
                </h1>
                {finding}
              </div>
              <div className={styles.titleSide}>
                <NextLook />
                <SourcesStatus />
              </div>
            </header>
            <div className={styles.mapArea} id="live-map">
              <MapPanel />
            </div>
          </div>
          <div className={styles.stripArea}>
            <PassStripPanel />
          </div>
          <div className={styles.side}>
            <UnseenList />
            <AirReceptors />
          </div>
          <div className={styles.console}>
            <InvestigatorSlot />
          </div>
        </main>
      </div>
    </LiveProvider>
  );
}
