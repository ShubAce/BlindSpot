import type { Metadata } from 'next';
import Link from 'next/link';
import { Suspense, type ReactNode } from 'react';

import { SiteNav } from '@/components/nav/SiteNav';
import { Accuracy, ArchitectureSection, BlindHours, HeroFrames, Limits, PassTimes, Risk, ScarRule, Smoke, Sources } from '@/components/method/sections';

import '@/components/case/app-screen.css';
import '@/components/method/method.css';

/**
 * /method: how BlindSpot works and how accurate it is, for a technical reader. The words describe the
 * method as the API and docs/architecture.md state it; every figure and chart is read from the API at
 * request time, section by section, each inside its own Suspense so the page's frame paints at once
 * and a slow endpoint holds back only its own section.
 */

export const metadata: Metadata = {
  title: 'Method and accuracy',
  description: 'How BlindSpot finds candidate unseen burns and the evening burning the polar fire satellites do not report, and how often each part is right: pass-time error, burn-scar precision, the smoke model against measured PM2.5, the risk backtest and every data source.',
};

const SECTIONS: { id: string; title: string; lead: string; body: () => ReactNode; tall?: number }[] = [
  {
    id: 'blind-hours',
    title: 'The blind hours',
    lead: "NASA's polar fire satellites look at Punjab in a few short windows a day. GK-2A, a geostationary weather satellite, scans every ten minutes. Its record shows when the fields burn, and how much of that no polar satellite sees.",
    body: () => <BlindHours />,
    tall: 760,
  },
  {
    id: 'pass-times',
    title: 'Pass times, checked',
    lead: "The pass clock on the live map is computed in your browser from CelesTrak orbital elements. The API checks the same prediction against the time NASA stamped on every fire detection.",
    body: () => <PassTimes />,
    tall: 520,
  },
  {
    id: 'burn-scars',
    title: 'Reading a burn scar',
    lead: 'A burnt field stays dark in Sentinel-2 pictures for days, whatever hour it was lit. The rule compares every clear pass with the clear pass before it, and then asks whether any fire alert explains what changed.',
    body: () => <ScarRule />,
    tall: 640,
  },
  {
    id: 'accuracy',
    title: 'How often it is right',
    lead: 'Every candidate unseen burn rests on that rule, so the rule was audited: scars drawn at random, each judged by eye from its pictures before and after.',
    body: () => <Accuracy />,
    tall: 620,
  },
  {
    id: 'smoke',
    title: 'Smoke against the air',
    lead: 'The smoke model carries particles from every fire detection and candidate unseen burn on the forecast winds. To check it, the API replays it over the last days and compares the smoke at each city with the PM2.5 its stations measured.',
    body: () => <Smoke />,
    tall: 820,
  },
  {
    id: 'risk',
    title: 'Ranking the next burn',
    lead: 'Fields that are harvested but not yet burnt are ranked by how likely they are to burn next, so a seeder or a visit can reach them first. The formula is open, and so is how well it has done.',
    body: () => <Risk />,
    tall: 900,
  },
  {
    id: 'limits',
    title: 'What it cannot do',
    lead: 'The limits, in the words the API publishes beside the numbers they qualify.',
    body: () => <Limits />,
    tall: 520,
  },
  {
    id: 'sources',
    title: 'Where the data comes from',
    lead: 'Every figure on this site comes from one of these, fetched by a scheduled job and stored with the moment it was fetched.',
    body: () => <Sources />,
    tall: 900,
  },
  {
    id: 'architecture',
    title: 'How the parts connect',
    lead: 'Jobs fetch and compute on a schedule and write to one database. The website and the investigator only ever read the API, so a slow upstream never slows a page; it only makes its numbers older, and the page says how old.',
    body: () => <ArchitectureSection />,
    tall: 520,
  },
];

function SectionSkeleton({ height }: { height: number }) {
  return <div className="m-skeleton" style={{ height }} aria-hidden="true" />;
}

export default function MethodPage() {
  return (
    <div className="method">
      <a className="skip-link" href="#main">
        Skip to the method
      </a>
      <SiteNav current="method" />
      <main id="main">
        <header className="m-hero">
          <h1 className="m-hero__title">How BlindSpot counts</h1>
          <div className="m-hero__side">
            <p className="m-hero__lead">The method behind every number on this site, and how often each part is right. Every figure below is read from the BlindSpot API as this page loads.</p>
            <nav aria-label="On this page" className="m-toc">
              <ul>
                {SECTIONS.map((s) => (
                  <li key={s.id}>
                    <a href={`#${s.id}`}>{s.title}</a>
                  </li>
                ))}
              </ul>
            </nav>
          </div>
        </header>
        <Suspense fallback={<div className="m-frames m-frames--loading" aria-hidden="true" />}>
          <HeroFrames />
        </Suspense>
        {SECTIONS.map((s) => (
          <section key={s.id} id={s.id} className="m-section" aria-labelledby={`${s.id}-h`}>
            <div className="m-section__head">
              <h2 id={`${s.id}-h`} className="m-section__title">
                {s.title}
              </h2>
              <p className="m-section__lead">{s.lead}</p>
            </div>
            <Suspense fallback={<SectionSkeleton height={s.tall ?? 480} />}>{s.body()}</Suspense>
          </section>
        ))}
      </main>
      <footer className="m-foot">
        <p className="m-foot__sign">Look up at five</p>
        <p className="m-foot__line">BlindSpot was built for Forge&apos;26. The code, the method and every data source are open.</p>
        <nav aria-label="More" className="m-foot__links">
          <Link href="/live">Live map</Link>
          <Link href="/bulletin">Today&apos;s bulletin</Link>
          <Link href="/">Home</Link>
        </nav>
        <p className="m-foot__imprint">Imagery: Copernicus Sentinel-2 (ESA), rendered by the BlindSpot API. Fire detections: NASA FIRMS. Times are India Standard Time.</p>
      </footer>
    </div>
  );
}
