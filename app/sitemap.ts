// path: app/sitemap.ts

// Next.js picks this up automatically at /sitemap.xml. Static routes are
// hardcoded below; server listings are pulled from the same public
// directory feed the site itself uses, capped generously since the feed's
// own page_size cap (60) means a full crawl needs several pages — for a
// directory this size that's a handful of extra requests at build/
// revalidate time, not a real cost.
import type { MetadataRoute } from 'next'

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://prime-bot.example.com'
const MAX_PAGES = 10 // 10 * 60 page_size = up to 600 listings indexed

async function fetchAllGuildIds(): Promise<string[]> {
  const ids: string[] = []
  for (let page = 1; page <= MAX_PAGES; page++) {
    try {
      const res = await fetch(`${API_BASE}/api/server_listings?page=${page}&page_size=60`, {
        next: { revalidate: 3600 },
      })
      const data = await res.json()
      if (data.status !== 'ok' || !Array.isArray(data.listings) || data.listings.length === 0) break
      ids.push(...data.listings.map((l: { guild_id: string }) => l.guild_id))
      if (ids.length >= (data.total || 0)) break
    } catch {
      break
    }
  }
  return ids
}

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const staticRoutes: MetadataRoute.Sitemap = [
    { url: `${SITE_URL}/`, changeFrequency: 'hourly', priority: 1 },
    { url: `${SITE_URL}/servers`, changeFrequency: 'hourly', priority: 0.9 },
    { url: `${SITE_URL}/servers/submit`, changeFrequency: 'monthly', priority: 0.3 },
  ]

  const guildIds = await fetchAllGuildIds()
  const listingRoutes: MetadataRoute.Sitemap = guildIds.map((id) => ({
    url: `${SITE_URL}/servers/${id}`,
    changeFrequency: 'daily',
    priority: 0.6,
  }))

  return [...staticRoutes, ...listingRoutes]
}
