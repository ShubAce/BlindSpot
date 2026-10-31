'use client';

import type { ReactNode } from 'react';

import { AirReceptors } from './AirReceptors';
import { IconRail } from './IconRail';
import { InvestigatorSlot } from './InvestigatorSlot';
import { LiveProvider } from './live-state';
import styles from './styles';
import { MapPanel } from './MapPanel';
import { NextLook } from './NextLook';
import { PassStripPanel } from './PassStripPanel';
import { SourcesStatus } from './SourcesStatus';
import { UnseenList } from './UnseenList';

/**
 * Mission control. The question it answers first: where did fields burn unseen since the last pass,
 * and where is that smoke going?
 *
 * Grid (1440 x 900, 16px gutters, rail 50px):
 *   columns  [main 1fr] [side 360px] ([console, its own width, only when the console renders])
 *   rows     [title auto] [map 1fr] [strip auto], the side column spans map and strip.
 *   1440 - 50 rail - 2 x 16 padding - 16 gap - 360 side = 982px of main column.
 * Below 1100px one column: title, map, strip, then the side panels two up; below 700px the rail
 * becomes a bottom bar and the ranked list a sheet.
 */
export function LiveScreen({ finding }: { finding: ReactNode }) {
  return (
    <LiveProvider>
      <div data-surface="app" className={styles.shell}>
        <a href="#live-map" className={styles.skip}>
          Skip to the map
        </a>
        <IconRail active="/live" />
        <main className={styles.screen} id="main">
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
