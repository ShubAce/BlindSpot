import { API_BASE, PATHS } from './api';
import { AOI_BBOX } from './aoi';

/**
 * The site's identity for search engines, link previews and structured data: one place for its
 * address, the words people search with, and the schema.org objects every page carries.
 *
 * NEXT_PUBLIC_SITE_URL is set at build time (infra/aws/deploy.py); the fallback is the production
 * address, so a build without it still produces absolute canonical and sitemap URLs.
 */
export const SITE_URL = (process.env.NEXT_PUBLIC_SITE_URL || 'https://www.blindspotin.tech').replace(/\/+$/, '');
export const REPO_URL = 'https://github.com/ShubAce/BlindSpot';

export const SITE_NAME = 'BlindSpot';

/** The link-preview defaults every page shares (a page adding its own `url` spreads these first). */
export const OPEN_GRAPH = { type: 'website', siteName: SITE_NAME, locale: 'en_IN' } as const;

export const SITE_DESCRIPTION =
  'Stubble (parali) burning in Punjab and Haryana, mapped every day: Sentinel-2 burn scars no NASA fire alert matched, the evening fires the polar satellites miss, and the smoke on its way to Delhi.';

/** What people type when they look for this: English, Hindi and Punjabi words for the same thing. */
export const KEYWORDS = [
  'stubble burning',
  'parali burning',
  'पराली जलाना',
  'ਪਰਾਲੀ ਸਾੜਨਾ',
  'crop residue burning',
  'farm fires',
  'Punjab stubble burning',
  'Haryana stubble burning',
  'stubble burning live map',
  'Punjab fire count',
  'Delhi air pollution',
  'Delhi AQI stubble',
  'burn scars',
  'Sentinel-2',
  'NASA FIRMS',
  'VIIRS',
  'MODIS',
  'GK-2A',
  'satellite fire detection',
  'PM2.5',
];

/** A schema.org object as a <script type="application/ld+json"> body: `<` escaped so a value can
 *  never close the script element (Next's JSON-LD guide). */
export function jsonLd(data: object): string {
  return JSON.stringify(data).replace(/</g, '\\u003c');
}

const ORGANIZATION = {
  '@type': 'Organization',
  '@id': `${SITE_URL}/#organization`,
  name: SITE_NAME,
  url: SITE_URL,
  logo: `${SITE_URL}/icons/icon-512.png`,
  sameAs: [REPO_URL],
};

/** On every page: who publishes the site, and the site itself. */
export const SITE_GRAPH = {
  '@context': 'https://schema.org',
  '@graph': [
    ORGANIZATION,
    {
      '@type': 'WebSite',
      '@id': `${SITE_URL}/#website`,
      url: SITE_URL,
      name: SITE_NAME,
      description: SITE_DESCRIPTION,
      inLanguage: ['en-IN', 'hi-IN', 'pa-IN'],
      publisher: { '@id': `${SITE_URL}/#organization` },
    },
  ],
};

/**
 * On the home page: the data BlindSpot publishes, described for Google Dataset Search. Every value
 * here is a fixed fact about the dataset (no figures, which live on the pages and change daily).
 */
export const DATASET = {
  '@context': 'https://schema.org',
  '@type': 'Dataset',
  '@id': `${SITE_URL}/#dataset`,
  name: 'BlindSpot: candidate unseen stubble burns in Punjab and Haryana',
  alternateName: 'Stubble (parali) burning that fire satellites miss, Punjab and Haryana',
  description:
    'Burn scars mapped from Sentinel-2 imagery in Punjab and Haryana, India, each matched against every NASA FIRMS fire alert (VIIRS and MODIS). A scar with no alert near it is a candidate unseen burn; a visual audit of random samples measures how many are real burns. Alongside: GK-2A geostationary fire pixels for the evening burning the polar satellites do not report, satellite pass predictions, smoke trajectories towards Delhi and other cities, and district-level field briefs. Updated through the burning season by scheduled jobs; every value carries its sources.',
  url: SITE_URL,
  sameAs: REPO_URL,
  keywords: KEYWORDS.filter((k) => /^[\x20-\x7e]+$/.test(k)),
  isAccessibleForFree: true,
  creator: { '@id': `${SITE_URL}/#organization` },
  publisher: { '@id': `${SITE_URL}/#organization` },
  spatialCoverage: {
    '@type': 'Place',
    name: 'Punjab, Haryana and Delhi, India',
    // schema.org's box is "south west north east" (lat lon lat lon); AOI_BBOX is [minLon, minLat, maxLon, maxLat]
    geo: { '@type': 'GeoShape', box: `${AOI_BBOX[1]} ${AOI_BBOX[0]} ${AOI_BBOX[3]} ${AOI_BBOX[2]}` },
  },
  variableMeasured: [
    'burn scar area (hectares)',
    'candidate unseen burn share',
    'fire detections (NASA FIRMS VIIRS and MODIS)',
    'GK-2A fire pixels per 10-minute scan',
    'PM2.5 at CPCB stations',
  ],
  measurementTechnique: [
    'Sentinel-2 differenced Normalized Burn Ratio (dNBR)',
    'NASA FIRMS active fire matching',
    'GK-2A AMI fire detection',
    'SGP4 orbit prediction',
    'Lagrangian smoke trajectories',
  ],
  distribution: [
    {
      '@type': 'DataDownload',
      name: 'Season summary by district (JSON)',
      encodingFormat: 'application/json',
      contentUrl: `${API_BASE}${PATHS.scarsSummary}`,
    },
    {
      '@type': 'DataDownload',
      name: 'Data sources and their freshness (JSON)',
      encodingFormat: 'application/json',
      contentUrl: `${API_BASE}${PATHS.sources}`,
    },
  ],
};
