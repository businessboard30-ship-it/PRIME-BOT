// path: app/rss.xml/route.ts

// Plain Next.js route handler (not a page) — GET /rss.xml returns an RSS 2.0
// feed of the newest listings, for external bots/aggregators that want to
// watch the directory without polling the JSON API and reimplementing
// pagination/sort themselves.
import { NextResponse } from 'next/server'

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://prime-bot.example.com'

function escapeXml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;')
}

export async function GET() {
  let listings: Array<{ guild_id: string; guild_name: string; description: string; updated_at?: string }> = []
  try {
    const res = await fetch(`${API_BASE}/api/server_listings?sort=newest&page=1&page_size=50`, {
      next: { revalidate: 300 },
    })
    const data = await res.json()
    if (data.status === 'ok') listings = data.listings
  } catch {
    listings = []
  }

  const items = listings
    .map((l) => {
      const link = `${SITE_URL}/servers/${l.guild_id}`
      const pubDate = l.updated_at ? new Date(l.updated_at).toUTCString() : new Date().toUTCString()
      return `
    <item>
      <title>${escapeXml(l.guild_name)}</title>
      <link>${link}</link>
      <guid>${link}</guid>
      <description>${escapeXml(l.description || '')}</description>
      <pubDate>${pubDate}</pubDate>
    </item>`
    })
    .join('')

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>PRIME-BOT Server Directory</title>
    <link>${SITE_URL}/servers</link>
    <description>Newest Discord servers listed on the PRIME-BOT directory</description>${items}
  </channel>
</rss>`

  return new NextResponse(xml, { headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' } })
}
