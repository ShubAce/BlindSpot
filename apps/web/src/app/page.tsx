import type { Metadata } from 'next';
import { Suspense, ViewTransition } from 'react';

import { Footer } from '@/components/landing/Footer';
import { Harvested } from '@/components/landing/Harvested';
import { HeroPictures } from '@/components/landing/HeroPictures';
import { Intro } from '@/components/landing/Intro';
import '@/components/landing/landing.css';
import { s } from '@/components/landing/styles';
import { OneDay } from '@/components/landing/OneDay';
import { Proof } from '@/components/landing/Proof';
import { HeroFoot, Rail } from '@/components/landing/Rail';
import { RailSentence, RailSentenceWords } from '@/components/landing/RailSentence';
import { Reply } from '@/components/landing/Reply';
import { Showcase } from '@/components/landing/Showcase';

export const metadata: Metadata = {
  title: { absolute: 'BlindSpot: the crop fires satellites miss' },
  description:
    'BlindSpot counts the crop-residue fires in Punjab and Haryana that the fire satellites miss, from Sentinel-2 burn scars, a live satellite pass clock, a smoke model and an investigator whose answers are cited and checked.',
};

/**
 * The landing page (.taste-skill/copy.md, direction "The place carries it"). The page shell (rail,
 * headings, the words that are not data) is static and paints at once; every figure, name, date and
 * picture is read from the BlindSpot API or NASA GIBS at request time inside its own <Suspense>, and
 * the live parts keep polling in the browser. No MapLibre or deck.gl here: those load on /live.
 */
export default function Home() {
  // The rail's sentence is read from the API; it streams in, with the same idea in words until then.
  const sentence = (
    <Suspense fallback={<RailSentenceWords />}>
      <RailSentence />
    </Suspense>
  );
  // Leaving for mission control (links typed 'to-live'), the page steps back and fades: one view
  // transition, nothing on any other navigation.
  return (
    <ViewTransition default="none" exit={{ 'to-live': 'lp-leave', default: 'none' }}>
      <div className={s.page}>
        <a href="#main" className={s.skip}>
          Skip to the content
        </a>
        <div className={s.pictures}>
          <Suspense fallback={<div className={s.picFallback} />}>
            <HeroPictures />
          </Suspense>
          <HeroFoot sentence={sentence} />
          <span id="hero-end" className={s.heroEnd} aria-hidden="true" />
        </div>
        <div className={s.lead} aria-hidden="true" />
        <Rail sentence={sentence} />
        <div className={s.tail} aria-hidden="true" />
        <main id="main">
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
