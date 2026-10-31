import type { MetadataRoute } from 'next';

/** Everything is public except the developer lab. */
export default function robots(): MetadataRoute.Robots {
  return { rules: [{ userAgent: '*', allow: '/', disallow: '/lab' }] };
}
