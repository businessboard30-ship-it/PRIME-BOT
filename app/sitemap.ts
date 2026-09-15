// path: app/sitemap.ts

// Next.js picks this up automatically at /sitemap.xml. Server-listing
// directory routes were removed from the site, so this is just the
// static routes that remain (manual unlock flow + dashboard entry point).
import type { MetadataRoute } from 'next'

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://prime-bot.example.com'

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: `${SITE_URL}/`, changeFrequency: 'hourly', priority: 1 },
    { url: `${SITE_URL}/unlock`, changeFrequency: 'monthly', priority: 0.5 },
  ]
}
