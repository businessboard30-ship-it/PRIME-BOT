// path: app/page.tsx

'use client'

import { Suspense } from 'react'
import Image from 'next/image'
import { useSearchParams } from 'next/navigation'
import ServerDirectory from './servers/_ServerDirectory'

export default function Page() {
  return (
    <main className="pb-page px-6 py-12">
      <Hero />

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

// Hero banner above the directory — 5 metallic angels art, PRIME LISTING badge.
function Hero() {
  return (
    <div className="relative -mx-6 -mt-12 mb-12 overflow-hidden" style={{ height: '52vh', minHeight: 360, maxHeight: 620 }}>
      <Image
        src="/hero-angels.png"
        alt="Cloaked knight with a glowing blue sword before a floating spire castle"
        fill
        priority
        sizes="100vw"
        className="object-cover"
        style={{ objectPosition: '30% center' }}
      />

      {/* darken left->right subtly + fade into page background so text on the right stays legible */}
      <div
        className="absolute inset-0"
        style={{
          background:
            'linear-gradient(90deg, rgba(5,6,10,0.15) 0%, rgba(5,6,10,0.05) 45%, rgba(5,6,10,0.55) 100%), linear-gradient(180deg, rgba(5,6,10,0.15) 0%, rgba(5,6,10,0.15) 65%, var(--pb-bg, #0a0b0d) 100%)',
        }}
      />

      <div className="relative h-full max-w-2xl mx-auto px-6 flex flex-col items-end justify-end text-right pb-10">
        <div
          className="inline-flex items-center gap-2 rounded-full border px-3 py-1 mb-4 backdrop-blur-sm"
          style={{
            borderColor: 'var(--pb-accent)',
            background: 'rgba(20,21,25,0.55)',
            boxShadow: '0 0 18px rgba(59,130,246,0.45)',
          }}
        >
          <span
            className="text-[10px] font-bold tracking-widest uppercase"
            style={{ color: 'var(--pb-accent)' }}
          >
            ⚔ Prime Listing
          </span>
        </div>

        <h1
          className="text-3xl sm:text-4xl font-bold leading-tight tracking-tight"
          style={{
            color: 'var(--pb-accent)',
            textShadow: '0 0 24px rgba(59,130,246,0.55), 0 2px 12px rgba(0,0,0,0.8)',
          }}
        >
          Find your next Discord server
        </h1>
        <p
          className="mt-2 text-sm sm:text-base max-w-md"
          style={{ color: 'rgba(229,231,235,0.85)', textShadow: '0 1px 6px rgba(0,0,0,0.9)' }}
        >
          A directory of communities running PRIME-BOT — admin-verified, ranked by votes.
        </p>
      </div>
    </div>
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
