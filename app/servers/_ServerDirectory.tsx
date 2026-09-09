// path: app/servers/_ServerDirectory.tsx

'use client'

/**
 * The actual server-listing directory — search box, sort/tag/NSFW filters,
 * paginated listing rows, per-listing report button, and the "list your
 * server" instructions. Extracted out of app/servers/page.tsx so
 * app/page.tsx (the homepage) can render the exact same directory as its
 * main content instead of duplicating this logic, per the site being a
 * server-listing site first — the homepage shouldn't be a separate bot
 * marketing page with the actual directory buried one click away.
 *
 * Rendered inside a <section>, not its own <main> — the caller (app/page.tsx
 * or app/servers/page.tsx) owns the page-level <main> wrapper.
 */

import { useEffect, useState } from 'react'
import BoostModal from './_BoostModal'
import { useSignedInUser } from '../_signedInSession'

type Listing = {
  guild_id: string
  clone_id: number | null
  guild_name: string
  guild_icon_url: string | null
  banner_url: string | null
  member_count: number
  invite_url: string
  description: string
  tags: string[]
  ref_code: string | null
  vote_count: number
  confirmed_conversions: number
  boost_count: number
  verified: boolean
}

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

// Rotating "fire" palette for listing cards — 10 colors, cycled by index
// so consecutive cards read as distinct without any per-server config.
const FIRE_COLORS = [
  { rgb: '59,130,246', hex: '#3b82f6' },  // blue
  { rgb: '239,68,68', hex: '#ef4444' },   // red
  { rgb: '168,85,247', hex: '#a855f7' },  // purple
  { rgb: '34,197,94', hex: '#22c55e' },   // green
  { rgb: '249,115,22', hex: '#f97316' },  // orange
  { rgb: '236,72,153', hex: '#ec4899' },  // pink
  { rgb: '234,179,8', hex: '#eab308' },   // gold
  { rgb: '20,184,166', hex: '#14b8a6' },  // teal
  { rgb: '139,92,246', hex: '#8b5cf6' },  // violet
  { rgb: '6,182,212', hex: '#06b6d4' },   // cyan
]
const DISCORD_CLIENT_ID = process.env.NEXT_PUBLIC_DISCORD_CLIENT_ID || ''
// Administrator (bit 8) — this bot's automod, roles, channels, and voice
// features span enough of Discord's permission surface that a hand-picked
// subset risks silently breaking something. Narrow this later once every
// permission this bot actually needs has been audited.
const BOT_INVITE_URL = DISCORD_CLIENT_ID
  ? `https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&scope=bot+applications.commands&permissions=8`
  : ''
const SUPPORT_SERVER_INVITE = 'https://discord.gg/DYfajXrP9B'
const PAGE_SIZE = 24
const POPULAR_CATEGORIES = ['gaming', 'anime', 'coding', 'art', 'music', 'study', 'crypto']

const SORTS: { value: string; label: string }[] = [
  { value: 'trending', label: 'Trending' },
  { value: 'votes', label: 'Most voted' },
  { value: 'members', label: 'Most members' },
  { value: 'newest', label: 'Newest' },
]

export default function ServerDirectory({ initialTag }: { initialTag?: string } = {}) {
  const [listings, setListings] = useState<Listing[] | null>(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('trending')
  const [tag, setTag] = useState<string | null>(initialTag || null)
  const [nsfw, setNsfw] = useState(false)
  const [page, setPage] = useState(1)
  const [loadingMore, setLoadingMore] = useState(false)
  const [voteBanner, setVoteBanner] = useState<{ ok: boolean; msg: string } | null>(null)
  const [reportingId, setReportingId] = useState<string | null>(null)
  const [boostingListing, setBoostingListing] = useState<Listing | null>(null)

  function fetchPage(pageNum: number, append: boolean) {
    if (append) setLoadingMore(true)
    const params = new URLSearchParams({
      sort, page: String(pageNum), page_size: String(PAGE_SIZE),
      nsfw: nsfw ? '1' : '0',
    })
    if (tag) params.set('tag', tag)
    fetch(`${API_BASE}/api/server_listings?${params.toString()}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== 'ok') {
          setError(data.message || 'Could not load the directory')
        } else {
          setTotal(data.total || 0)
          setListings((prev) => (append && prev ? [...prev, ...data.listings] : data.listings))
        }
      })
      .catch(() => setError('Network error loading the directory'))
      .finally(() => setLoadingMore(false))
  }

  // Re-fetch from page 1 whenever a filter changes.
  useEffect(() => {
    setPage(1)
    setListings(null)
    fetchPage(1, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, tag, nsfw])

  useEffect(() => {
    // Two independent things a landing URL can carry, both fire-and-forget:
    //  - ?ref=<code> — someone followed a listing's boost link; log the
    //    click server-side (see api/server_listings.py's Mode 0).
    //  - ?vote=ok|err&msg=... — the redirect back from
    //    api/server_listing_vote_oauth.py after a vote sign-in attempt.
    const params = new URLSearchParams(window.location.search)
    const ref = params.get('ref')
    if (ref) {
      fetch(`${API_BASE}/api/server_listings?ref=${encodeURIComponent(ref)}`).catch(() => {})
    }
    const voteResult = params.get('vote')
    const voteMsg = params.get('msg')
    if (voteResult && voteMsg) {
      setVoteBanner({ ok: voteResult === 'ok', msg: voteMsg })
    }
    if (ref || voteResult) {
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  async function submitReport(guildId: string) {
    const reason = window.prompt('Why are you reporting this server? (dead invite, TOS violation, etc.)')
    if (!reason || !reason.trim()) return
    setReportingId(guildId)
    try {
      await fetch(`${API_BASE}/api/server_listings?report=1&guild_id=${guildId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reason.trim() }),
      })
      window.alert('Thanks — report sent.')
    } catch {
      window.alert('Network error sending report — try again later.')
    } finally {
      setReportingId(null)
    }
  }

  const allTags = Array.from(new Set((listings || []).flatMap((l) => l.tags))).filter((t) => t !== 'nsfw').slice(0, 12)
  const filtered = (listings || []).filter((l) => {
    if (!query.trim()) return true
    const q = query.trim().toLowerCase()
    return l.guild_name.toLowerCase().includes(q) || l.description.toLowerCase().includes(q) || l.tags.some((t) => t.includes(q))
  })
  const canLoadMore = (listings?.length || 0) < total
  const { user, signOut } = useSignedInUser()

  return (
    <section>
      {voteBanner && (
        <div
          className="mb-6 rounded-lg border p-3 text-sm"
          style={{
            borderColor: voteBanner.ok ? 'var(--pb-positive)' : 'var(--pb-danger)',
            color: voteBanner.ok ? 'var(--pb-positive)' : 'var(--pb-danger)',
          }}
        >
          {voteBanner.msg}
        </div>
      )}

      <div className="flex items-center justify-between gap-4 mb-4">
        <h1
          className="text-2xl font-semibold"
          style={{ color: 'var(--pb-accent)', textShadow: '0 0 16px rgba(59,130,246,0.5)' }}
        >
          Server directory
        </h1>
        <div className="flex items-center gap-2 shrink-0">
          {user ? (
            <div className="flex items-center gap-2">
              <img
                src={user.avatar_url}
                alt=""
                className="w-7 h-7 rounded-full"
                style={{ border: '1px solid var(--pb-line)' }}
              />
              <span className="text-sm font-medium hidden sm:inline">{user.username}</span>
              <button
                onClick={signOut}
                className="text-sm px-3 py-1.5 rounded-md font-medium"
                style={{ border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
              >
                Sign out
              </button>
            </div>
          ) : (
            <a
              href={`${API_BASE}/api/discord_login_oauth`}
              className="pb-btn-primary text-sm"
            >
              Sign in
            </a>
          )}
          <a href={BOT_INVITE_URL || SUPPORT_SERVER_INVITE} className="pb-btn-primary text-sm">
            Add PRIME-BOT
          </a>
        </div>
      </div>

      <input
        className="w-full mb-3 rounded-lg px-4 py-2.5 text-sm outline-none transition-shadow"
        style={{
          background: 'rgba(13,16,24,0.85)',
          border: '1px solid var(--pb-accent)',
          color: '#e5e7eb',
          boxShadow:
            '0 0 0 1px rgba(59,130,246,0.25), 0 0 18px rgba(59,130,246,0.35), inset 0 0 24px rgba(59,130,246,0.08)',
        }}
        onFocus={(e) => {
          e.currentTarget.style.boxShadow =
            '0 0 0 1px rgba(59,130,246,0.55), 0 0 28px rgba(59,130,246,0.6), inset 0 0 30px rgba(59,130,246,0.15)'
        }}
        onBlur={(e) => {
          e.currentTarget.style.boxShadow =
            '0 0 0 1px rgba(59,130,246,0.25), 0 0 18px rgba(59,130,246,0.35), inset 0 0 24px rgba(59,130,246,0.08)'
        }}
        placeholder="Search servers, tags, descriptions…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {!initialTag && (
        <div className="flex flex-wrap gap-2 mb-4">
          {POPULAR_CATEGORIES.map((c) => (
            <a key={c} href={`/servers/category/${c}`} className="pb-chip text-xs">
              #{c}
            </a>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select className="pb-input text-sm w-auto" value={sort} onChange={(e) => setSort(e.target.value)}>
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>{s.label}</option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-xs cursor-pointer" style={{ color: 'var(--pb-text-faint)' }}>
          <input type="checkbox" checked={nsfw} onChange={(e) => setNsfw(e.target.checked)} />
          Show NSFW only
        </label>
        {tag && (
          <button className="pb-chip" onClick={() => setTag(null)}>
            #{tag}
            <span className="pb-chip-remove">×</span>
          </button>
        )}
      </div>

      {allTags.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-6">
          {allTags.map((t) => (
            <button
              key={t}
              className="pb-chip"
              style={tag === t ? { borderColor: 'var(--pb-accent)' } : undefined}
              onClick={() => setTag(tag === t ? null : t)}
            >
              #{t}
            </button>
          ))}
        </div>
      )}

      {error && <p className="text-sm" style={{ color: 'var(--pb-danger)' }}>{error}</p>}

      {!error && listings === null && (
        <div className="space-y-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="pb-skeleton-row" />
          ))}
        </div>
      )}

      {!error && listings !== null && filtered.length === 0 && (
        <p className="text-sm" style={{ color: 'var(--pb-text-faint)' }}>No servers match yet — be the first!</p>
      )}

      {!error && filtered.length > 0 && (
        <ul className="space-y-3">
          {filtered.map((l, i) => {
          const fire = FIRE_COLORS[i % FIRE_COLORS.length]
          return (
            <li
              key={l.guild_id}
              className="rounded-lg border p-4"
              style={{
                borderColor: fire.hex,
                background: 'var(--pb-surface)',
                boxShadow: `0 0 0 1px rgba(${fire.rgb},0.25), 0 0 20px rgba(${fire.rgb},0.35), inset 0 0 30px rgba(${fire.rgb},0.06)`,
              }}
            >
              <div className="flex items-start gap-3">
                {l.guild_icon_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={l.guild_icon_url} alt="" className="w-10 h-10 rounded-full shrink-0" />
                ) : (
                  <div
                    className="w-10 h-10 rounded-full flex items-center justify-center shrink-0"
                    style={{ background: 'var(--pb-surface-raised)', color: 'var(--pb-text-faint)' }}
                  >
                    {l.guild_name.slice(0, 1).toUpperCase()}
                  </div>
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <a href={`/servers/${l.guild_id}`} className="font-medium hover:underline">{l.guild_name}</a>
                    {l.verified && (
                      <span className="text-xs" title="Verified" style={{ color: 'var(--pb-accent)' }}>✓ Verified</span>
                    )}
                  </div>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--pb-text-faint)' }}>
                    {l.member_count.toLocaleString()} members · {l.vote_count} votes
                  </p>
                  {l.description && (
                    <p className="text-sm mt-2" style={{ color: 'var(--pb-text-muted)' }}>{l.description}</p>
                  )}
                  {l.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {l.tags.map((t) => (
                        <button key={t} className="pb-chip text-xs" onClick={() => setTag(t)}>#{t}</button>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex flex-col items-end gap-2 shrink-0">
                  <a href={l.invite_url} target="_blank" rel="noopener noreferrer" className="pb-btn-secondary text-sm">
                    Join
                  </a>
                  <a
                    href={`${API_BASE}/api/server_listing_vote_oauth?guild_id=${l.guild_id}${l.clone_id ? `&clone_id=${l.clone_id}` : ''}`}
                    className="text-sm px-3 py-1.5 rounded-md font-medium text-center"
                    style={{
                      background: `rgba(${fire.rgb},0.15)`,
                      border: `1px solid ${fire.hex}`,
                      color: fire.hex,
                    }}
                  >
                    ▲ Vote
                  </a>
                  <button
                    className="text-sm px-3 py-1.5 rounded-md font-medium"
                    style={{ background: 'rgba(234,179,8,0.15)', color: '#eab308', border: '1px solid rgba(234,179,8,0.4)' }}
                    onClick={() => setBoostingListing(l)}
                  >
                    ⚡ Boost
                  </button>
                  <button
                    className="text-xs underline"
                    style={{ color: 'var(--pb-text-faint)' }}
                    disabled={reportingId === l.guild_id}
                    onClick={() => submitReport(l.guild_id)}
                  >
                    Report
                  </button>
                </div>
              </div>
            </li>
          )})}
        </ul>
      )}

      {canLoadMore && (
        <button
          className="pb-btn-secondary w-full mt-4"
          disabled={loadingMore}
          onClick={() => {
            const next = page + 1
            setPage(next)
            fetchPage(next, true)
          }}
        >
          {loadingMore ? 'Loading…' : `Load more (${listings?.length || 0} of ${total})`}
        </button>
      )}

      <section className="mt-10 pt-6 border-t text-sm" style={{ borderColor: 'var(--pb-line)', color: 'var(--pb-text-faint)' }}>
        Want your server listed? Run <code className="pb-code">/setup servers</code> in your Discord server
        (PRIME-BOT must already be a member) to get your private listing link.
      </section>

      {boostingListing && (
        <BoostModal
          guildName={boostingListing.guild_name}
          guildId={boostingListing.guild_id}
          cloneId={boostingListing.clone_id}
          refCode={boostingListing.ref_code}
          onClose={() => setBoostingListing(null)}
          onBoosted={(newBoostCount) => {
            // Reflect the boost immediately in the list the user is
            // looking at. The real ranking change (trending sort now
            // weighing sl.boost_count — see database.py) takes effect
            // on the next fetch from the server; this just avoids the
            // card looking unchanged right after a successful boost.
            setListings((prev) =>
              prev
                ? prev.map((l) =>
                    l.guild_id === boostingListing.guild_id ? { ...l, boost_count: newBoostCount } : l
                  )
                : prev
            )
            setVoteBanner({ ok: true, msg: `Boost applied to ${boostingListing.guild_name}.` })
          }}
        />
      )}
    </section>
  )
}
