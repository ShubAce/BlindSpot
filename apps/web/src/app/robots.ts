import type { MetadataRoute } from 'next';

import { SITE_URL } from '@/lib/site';

/** Everything is public except the developer lab and the offline stand-in; the sitemap lists the
 *  pages worth finding (app/sitemap.ts). */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: '*', allow: '/', disallow: ['/lab', '/offline'] }],
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
