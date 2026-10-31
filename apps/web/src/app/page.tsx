import type { Metadata } from 'next';
import { Suspense, ViewTransition } from 'react';

import { Footer } from '@/components/landing/Footer';
import { Harvested } from '@/components/landing/Harvested';
import { HeroClock, HeroFigure } from '@/components/landing/Hero';
import { HeroPictures } from '@/components/landing/HeroPictures';
import { Intro } from '@/components/landing/Intro';
import '@/components/landing/landing.css';
import { s } from '@/components/landing/styles';
import { OneDay } from '@/components/landing/OneDay';
import { Proof } from '@/components/landing/Proof';
import { Rail } from '@/components/landing/Rail';
import { Reply } from '@/components/landing/Reply';
import { Showcase } from '@/components/landing/Showcase';

export const metadata: Metadata = {
  title: { absolute: 'BlindSpot: the crop fires satellites miss' },
  description:
    'BlindSpot counts the crop-residue fires in Punjab and Haryana that the fire satellites miss, from Sentinel-2 burn scars, a live clock of when the satellites look, a smoke model and an investigator whose answers are cited and checked.',
};

/**
 * The landing page (.taste-skill/copy.md, direction "The place carries it"). The header and the words
 * that are not data paint at once; every figure, name, date and picture is read from the BlindSpot API
 * or NASA GIBS at request time inside its own <Suspense>, and the live parts keep moving in the
 * browser. No MapLibre or deck.gl here: those load on /live.
 *
 * The first screen holds the argument whole: the name, the key figure (the share of burnt area no fire
 * alert explains, with its audit), the blind-spot clock (when the satellites look, when the fields
 * burn) and the evidence (one place on four Sentinel-2 passes, its burn scars outlined as they appear).
 */
export default function Home() {
  // Leaving for mission control (links typed 'to-live'), the page steps back and fades: one view
  // transition, nothing on any other navigation.
  return (
    <ViewTransition default="none" exit={{ 'to-live': 'lp-leave', default: 'none' }}>
      <div className={s.page}>
        <a href="#main" className={s.skip}>
          Skip to the content
        </a>
        <Rail />
        <main id="main">
          <section className={s.hero} aria-label="The unseen share, the clock of the satellites’ looks, and one place on four passes">
            <div className={s.heroFigure}>
              <Suspense fallback={<div className={s.heroFigureSkeleton} />}>
                <HeroFigure />
              </Suspense>
            </div>
            <div className={s.heroClock}>
              <Suspense fallback={<div className={s.clockSkeleton} />}>
                <HeroClock />
              </Suspense>
            </div>
            <div className={s.heroPic}>
              <Suspense fallback={<div className={s.picFallback} />}>
                <HeroPictures />
              </Suspense>
            </div>
          </section>
          <Intro />
          <Showcase />
          <Harvested />
          <OneDay />
          <Proof />
          <Reply />
        </main>
        <Footer />
      </div>
    </ViewTransition>
  );
}
