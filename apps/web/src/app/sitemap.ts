import type { MetadataRoute } from 'next';

import { getBriefIndex } from '@/components/brief/data';
import { DEFAULT_DAYS, districtHref } from '@/components/brief/read';
import { getUnseenList, onePerPlace } from '@/components/landing/data';
import { toDate, type DateInput } from '@/lib/format';
import { SITE_URL } from '@/lib/site';

/**
 * /sitemap.xml: the pages search engines should know about. The five main pages, every district's
 * field brief, and the case files of the landing page's candidate unseen burns (one per village,
 * largest first), so a search for a district or a village's burning can land on its own page.
 *
 * The district and case lists are read from the API at request time (serverGet's short cache makes
 * this a request-time route under Cache Components; the case list is the landing page's own read), so
 * the sitemap follows the season without a rebuild. If the API does not answer, the main pages are
 * still listed.
 */

const CASES = 100;
/** The brief index's longest window lists every district that burned in the last month, so a
 *  district's page stays in the sitemap through a quiet week (the URLs themselves carry no window). */
const DISTRICT_DAYS = 30;

type Frequency = 'hourly' | 'daily' | 'weekly';

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const now = new Date();
  const page = (path: string, changeFrequency: Frequency, priority: number, modified?: DateInput) => ({
    url: `${SITE_URL}${path}`,
    lastModified: toDate(modified) ?? now,
    changeFrequency,
    priority,
  });

  const entries: MetadataRoute.Sitemap = [
    page('/', 'hourly', 1),
    page('/live', 'hourly', 0.9),
    page('/bulletin', 'daily', 0.8),
    page('/brief', 'daily', 0.8),
    page('/method', 'weekly', 0.7),
  ];

  const [index, scars] = await Promise.all([getBriefIndex(DISTRICT_DAYS), getUnseenList()]);

  if (index.ok) {
    const seen = new Set<string>();
    for (const row of index.data.districts) {
      const href = districtHref(row.district, DEFAULT_DAYS);
      if (seen.has(href)) continue;
      seen.add(href);
      entries.push(page(href, 'daily', 0.6, row.newest_pass));
    }
  }

  if (scars.ok) {
    for (const p of onePerPlace(scars.data, CASES)) entries.push(page(`/case/${p.id}`, 'weekly', 0.4, p.burn_before));
  }

  return entries;
}
