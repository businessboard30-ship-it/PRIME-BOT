// path: app/servers/[guildId]/page.tsx

// Server component (no 'use client') so generateMetadata can run at
// request time and produce real Open Graph tags per listing — Discord/
// Twitter/etc. unfurl this URL using these tags, not the client-rendered
// directory below. The actual visible page content is intentionally the
// same shape as a directory row rather than a heavier profile, since all
// the site knows about a server is what server_listings already stores.

import type { Metadata } from 'next'

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://prime-bot.example.com'

type Listing = {
  guild_id: string
  guild_name: string
  guild_icon_url: string | null
  banner_url: string | null
  member_count: number
  invite_url: string
  description: string
  long_description: string | null
  tags: string[]
  vote_count: number
  verified: boolean
  clone_id: number | null
  ref_code: string | null
}

async function fetchListing(guildId: string): Promise<Listing | null> {
  try {
    const res = await fetch(`${API_BASE}/api/server_listings?listing_guild_id=${guildId}`, {
      // Directory data changes with votes/edits — don't let Next cache a
      // stale vote count or invite link indefinitely.
      next: { revalidate: 60 },
    })
    const data = await res.json()
    if (data.status !== 'ok') return null
    return data.listing
  } catch {
    return null
  }
}

async function fetchLeaderboard(guildId: string): Promise<{ voter_id: string; voter_username: string | null }[]> {
  try {
    const res = await fetch(`${API_BASE}/api/server_listings?leaderboard_guild_id=${guildId}`, { next: { revalidate: 300 } })
    const data = await res.json()
    return data.status === 'ok' ? data.voters : []
  } catch {
    return []
  }
}

async function fetchSimilar(guildId: string): Promise<Listing[]> {
  try {
    const res = await fetch(`${API_BASE}/api/server_listings?similar_to_guild_id=${guildId}`, { next: { revalidate: 300 } })
    const data = await res.json()
    return data.status === 'ok' ? data.listings : []
  } catch {
    return []
  }
}

export async function generateMetadata({ params }: { params: Promise<{ guildId: string }> }): Promise<Metadata> {
  const { guildId } = await params
  const listing = await fetchListing(guildId)
  if (!listing) {
    return { title: 'Server not found — PRIME-BOT directory' }
  }
  const title = `${listing.guild_name} — Discord server`
  const description = listing.description || `${listing.member_count.toLocaleString()} members. Join on the PRIME-BOT server directory.`
  const image = listing.banner_url || listing.guild_icon_url || undefined
  return {
    title,
    description,
    openGraph: {
      title,
      description,
      url: `${SITE_URL}/servers/${listing.guild_id}`,
      images: image ? [{ url: image }] : undefined,
      type: 'website',
    },
    twitter: {
      card: image ? 'summary_large_image' : 'summary',
      title,
      description,
      images: image ? [image] : undefined,
    },
  }
}

export default async function ListingPage({ params }: { params: Promise<{ guildId: string }> }) {
  const { guildId } = await params
  const listing = await fetchListing(guildId)

  if (!listing) {
    return (
      <main className="pb-page px-6 py-12">
        <div className="max-w-xl mx-auto">
          <p className="text-sm" style={{ color: 'var(--pb-danger)' }}>
            No listing found for that server —{' '}
            <a href="/servers" className="underline">back to the directory</a>.
          </p>
        </div>
      </main>
    )
  }

  const [voters, similar] = await Promise.all([
    fetchLeaderboard(listing.guild_id),
    fetchSimilar(listing.guild_id),
  ])

  return (
    <main className="pb-page px-6 py-12">
      <div className="max-w-xl mx-auto">
        {listing.banner_url && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={listing.banner_url} alt="" className="w-full rounded-lg mb-6 object-cover" style={{ maxHeight: 200 }} />
        )}
        <div className="flex items-start gap-3 mb-4">
          {listing.guild_icon_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={listing.guild_icon_url} alt="" className="w-16 h-16 rounded-full" />
          ) : (
            <div
              className="w-16 h-16 rounded-full flex items-center justify-center text-2xl"
              style={{ background: 'var(--pb-surface-raised)', color: 'var(--pb-text-faint)' }}
            >
              {listing.guild_name.slice(0, 1).toUpperCase()}
            </div>
          )}
          <div>
            <h1 className="pb-heading text-2xl font-semibold flex items-center gap-2">
              {listing.guild_name}
              {listing.verified && <span className="text-sm" style={{ color: 'var(--pb-accent)' }}>✓ Verified</span>}
            </h1>
            <p className="text-sm mt-1" style={{ color: 'var(--pb-text-faint)' }}>
              {listing.member_count.toLocaleString()} members · {listing.vote_count} votes
            </p>
          </div>
        </div>

        {listing.description && (
          <p className="text-sm leading-relaxed mb-4" style={{ color: 'var(--pb-text-muted)' }}>
            {listing.description}
          </p>
        )}

        {listing.long_description && (
          <p className="text-sm leading-relaxed mb-6 whitespace-pre-wrap" style={{ color: 'var(--pb-text-muted)' }}>
            {listing.long_description}
          </p>
        )}

        {listing.tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-6">
            {listing.tags.map((t) => (
              <span key={t} className="pb-chip text-xs">#{t}</span>
            ))}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <a href={listing.invite_url} target="_blank" rel="noopener noreferrer" className="pb-btn-primary">
            Join server
          </a>

          {/* Sends the visitor through the same OAuth vote flow used on the
              directory cards (api/server_listing_vote_oauth.py), so a vote
              cast from the listing page counts the same as one from /servers. */}
          <a
            href={`${API_BASE}/api/server_listing_vote_oauth?guild_id=${listing.guild_id}${listing.clone_id ? `&clone_id=${listing.clone_id}` : ''}`}
            className="pb-btn-secondary text-sm"
          >
            Vote for this server
          </a>

          {/* Referral link back to the directory — carries this listing's
              ref_code so a click is attributed the same way boost links are
              (see _BoostModal.tsx's copyBoostLink and api/server_listings.py
              Mode 0). Only rendered when the listing actually has a code. */}
          {listing.ref_code && (
            <a
              href={`${SITE_URL}/servers?ref=${listing.ref_code}`}
              className="pb-btn-secondary text-sm"
            >
              Refer friends to the site
            </a>
          )}
        </div>

        {voters.length > 0 && (
          <section className="mt-10 pt-6 border-t" style={{ borderColor: 'var(--pb-line)' }}>
            <h2 className="pb-heading text-base font-medium mb-3">Recent voters</h2>
            <ul className="space-y-1.5 text-sm" style={{ color: 'var(--pb-text-muted)' }}>
              {voters.map((v) => (
                <li key={v.voter_id}>{v.voter_username || 'A voter'}</li>
              ))}
            </ul>
          </section>
        )}

        {similar.length > 0 && (
          <section className="mt-10 pt-6 border-t" style={{ borderColor: 'var(--pb-line)' }}>
            <h2 className="pb-heading text-base font-medium mb-3">Similar servers</h2>
            <ul className="space-y-3">
              {similar.map((s) => (
                <li key={s.guild_id} className="rounded-lg border p-3" style={{ borderColor: 'var(--pb-line)' }}>
                  <a href={`/servers/${s.guild_id}`} className="font-medium hover:underline">{s.guild_name}</a>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--pb-text-faint)' }}>
                    {s.member_count.toLocaleString()} members
                  </p>
                </li>
              ))}
            </ul>
          </section>
        )}

        <p className="text-xs mt-8">
          <a href="/servers" className="underline" style={{ color: 'var(--pb-text-faint)' }}>← Back to the directory</a>
        </p>
      </div>
    </main>
  )
}
