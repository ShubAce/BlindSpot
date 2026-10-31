import Link from 'next/link';
import { Suspense } from 'react';

import { absoluteApiUrl } from '@/lib/api';
import { dateIST } from '@/lib/format';
import { PHOTOS, type PhotoCredit } from '@/lib/photo-credits';

import { getFeatured } from './data';
import { s } from './styles';
import { Pic } from './Pic';

/** The photographs this page shows, in the order they appear: their credits go in the imprint. */
const USED: PhotoCredit[] = [PHOTOS.rakingStubble, PHOTOS.seederTractor, PHOTOS.strawBurning, PHOTOS.delhiSmog, PHOTOS.happySeeder];

const REPO = 'https://github.com/ShubAce/BlindSpot';

/**
 * Section 7, footer (direction: "The back of the card"): a sign-off, the addresses, a Sentinel-2
 * stamp, and the imprint with every picture credit.
 */
export function Footer() {
  return (
    <footer className={s.footer} aria-labelledby="footer-h">
      <div className={s.back}>
        <svg className={s.scallops} viewBox="0 0 1200 50" preserveAspectRatio="none" aria-hidden="true">
          <path d={scallopPath(12)} fill="currentColor" />
        </svg>
        <svg className={`${s.scallops} ${s.scallopsPhone}`} viewBox="0 0 1200 100" preserveAspectRatio="none" aria-hidden="true">
          <path d={scallopPath(6, 100)} fill="currentColor" />
        </svg>
        <div className={s.backMessage}>
          <h2 id="footer-h" className={s.backTitle}>
            Look between the passes
          </h2>
          <p>BlindSpot was built for Forge’26. The code, the method and every data source are open.</p>
        </div>
        <div className={s.backAddress}>
          <Suspense fallback={null}>
            <Stamp />
          </Suspense>
          <ul className={s.addressLines}>
            <li>
              <a href={REPO} rel="noreferrer" target="_blank">
                GitHub repository
              </a>
            </li>
            <li>
              <Link href="/method">Method and accuracy</Link>
            </li>
            <li>
              <a href="#sources">Data sources</a>
            </li>
            <li>
              <Link href="/live">Mission control</Link>
            </li>
          </ul>
        </div>
        <p className={s.imprint} id="credits">
          {'Imagery: Copernicus Sentinel-2 (ESA) via the BlindSpot API; NASA GIBS (VIIRS, MODIS). Photographs: '}
          {USED.map((p, i) => (
            <span key={p.src}>
              <a href={p.page} rel="noreferrer" target="_blank">
                {`“${p.title}”`}
              </a>
              {` by ${p.creator}, `}
              <a href={p.licenseUrl} rel="noreferrer" target="_blank">
                {p.license}
              </a>
              {i < USED.length - 1 ? '; ' : '.'}
            </span>
          ))}
          {' Built for Forge’26.'}
        </p>
      </div>
    </footer>
  );
}

/** A Sentinel-2 stamp: the newest featured pass, cropped 3:4. */
async function Stamp() {
  const r = await getFeatured();
  const f = r.ok ? r.data.frames?.[r.data.frames.length - 1] : null;
  const src = absoluteApiUrl(f?.truecolor_url);
  // No pass to show: no stamp, rather than an empty white square.
  if (!f || !src) return null;
  return (
    <span className={s.stamp}>
      <Pic src={src} width={f.width} height={f.height} alt={`Stamp: Sentinel-2 crop of the featured fields, ${dateIST(f.datetime ?? f.date, { year: true })}.`} className={s.cover} failText="Stamp did not load." />
    </span>
  );
}

/** A row of n half-ellipses along the top edge of a 1200 x h box (half-circles when h = 600 / n). */
function scallopPath(n: number, h = 50): string {
  const w = 1200 / n;
  const r = w / 2;
  let d = `M0 ${h} L0 ${h}`;
  for (let i = 0; i < n; i++) d += ` A${r} ${h} 0 0 1 ${(i + 1) * w} ${h}`;
  return `${d} L1200 ${h} Z`;
}
