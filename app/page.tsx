// path: app/page.tsx

'use client'

import { Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import ServerDirectory from './servers/_ServerDirectory'

export default function Page() {
  return (
    <main className="pb-page px-6 py-12">
      <div className="max-w-2xl mx-auto">
        <Suspense fallback={null}>
          <ListingBanner />
        </Suspense>

        <ServerDirectory />

        <section className="mt-16 pt-8 border-t" style={{ borderColor: 'var(--pb-line)' }}>
          <h2 className="pb-heading text-base font-medium">About this site</h2>
          <p className="mt-2 text-sm leading-relaxed max-w-lg" style={{ color: 'var(--pb-text-muted)' }}>
            This is a directory of public Discord servers running PRIME-BOT — a moderation,
            leveling, and community-management bot. Every server here is admin-verified
            automatically, since a listing can only be created from inside a server the bot
            is already in.
          </p>
        </section>
      </div>
    </main>
  )
}

// Shown when /setup servers sent someone here with listing params attached
// (see discord_bot/cogs/server_listing.py) — lets an admin who just ran that
// command jump straight to finishing their listing, above the directory.
function ListingBanner() {
  const params = useSearchParams()
  const guildId = params.get('guild_id')
  const token = params.get('token')
  if (!guildId || !token) return null

  const submitUrl = `/servers/submit?${params.toString()}`

  return (
    <div
      className="mb-8 flex flex-wrap items-center justify-between gap-4 rounded-lg border p-4"
      style={{ borderColor: 'var(--pb-accent)', background: 'var(--pb-surface)' }}
    >
      <div>
        <p className="text-sm font-medium">Your server's ready to list.</p>
        <p className="mt-1 text-xs" style={{ color: 'var(--pb-text-faint)' }}>
          This link is private to your server — continue to finish your listing.
        </p>
      </div>
      <a href={submitUrl} className="pb-btn-primary shrink-0">
        Continue to your listing
      </a>
    </div>
  )
}
