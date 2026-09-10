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

import { useEffect, useRef, useState } from 'react'
import BoostModal from './_BoostModal'
import { useSignedInUser } from '../_signedInSession'
import AddServerButton from './_AddServerButton'

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
  // Both populated by discord_bot/cogs/listing_snapshots.py's periodic
  // sweep — online_count is null until that job has reached this guild at
  // least once; member_count_trend/_delta are null until a snapshot at
  // least ~20h old exists, so a brand-new listing shows no arrow rather
  // than a fabricated "flat".
  online_count: number | null
  member_count_trend: 'up' | 'down' | 'flat' | null
  member_count_delta: number | null
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
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [sort, setSort] = useState('trending')
  const [tag, setTag] = useState<string | null>(initialTag || null)
  // Multi-select tags (AND'd together server-side), separate from the
  // single `tag` set by a category chip/URL so /servers/category/[tag]
  // links keep working unchanged.
  const [extraTags, setExtraTags] = useState<string[]>([])
  const [nsfw, setNsfw] = useState(false)
  const [page, setPage] = useState(1)
  const [loadingMore, setLoadingMore] = useState(false)
  const [voteBanner, setVoteBanner] = useState<{ ok: boolean; msg: string } | null>(null)
  const [reportingId, setReportingId] = useState<string | null>(null)
  const [boostingListing, setBoostingListing] = useState<Listing | null>(null)
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [qrGuildId, setQrGuildId] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [viewMode, setViewMode] = useState<'list' | 'grid'>('list')

  // --- Command palette (⌘K) --------------------------------------------
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState('')
  const [paletteResults, setPaletteResults] = useState<Listing[]>([])
  const [paletteActive, setPaletteActive] = useState(0)
  const [paletteLoading, setPaletteLoading] = useState(false)

  // --- Infinite scroll + lightweight virtualization ----------------------
  // No virtualization library in this project's deps, so this is a small
  // hand-rolled windower: it estimates a per-item height (which differs
  // between list/grid) and only mounts the slice of `filtered` whose
  // estimated position falls within [scrollTop - buffer, scrollTop +
  // viewport + buffer]. Above/below are two spacer divs sized to the
  // remaining estimated height so native scrollbar length stays correct.
  const scrollRootRef = useRef<HTMLDivElement | null>(null)
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  const [viewportRange, setViewportRange] = useState({ start: 0, end: 40 })
  const ITEM_HEIGHT = { list: 168, grid: 280 } as const
  const GRID_COLS = 3

  // Debounce free-text search so every keystroke doesn't fire a request —
  // this now hits the full server-wide dataset via ?q=, not just the
  // listings already loaded on the current page.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim()), 350)
    return () => clearTimeout(t)
  }, [query])

  // Global ⌘K / Ctrl+K to open the quick-jump palette from anywhere on the
  // page, plus Escape to close it. Ignored while the user is already typing
  // in an input/textarea other than the palette's own box.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const isCmdK = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k'
      if (isCmdK) {
        e.preventDefault()
        setPaletteOpen((cur) => !cur)
        return
      }
      if (e.key === 'Escape') setPaletteOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // Debounced server-wide search that backs the palette — independent of
  // the main directory's own query/debouncedQuery so opening the palette
  // never disturbs the filters already applied to the page behind it.
  useEffect(() => {
    if (!paletteOpen) return
    const q = paletteQuery.trim()
    if (!q) {
      setPaletteResults([])
      return
    }
    setPaletteLoading(true)
    const t = setTimeout(() => {
      const params = new URLSearchParams({ sort: 'trending', page: '1', page_size: '8', nsfw: '0', q })
      fetch(`${API_BASE}/api/server_listings?${params.toString()}`)
        .then((r) => r.json())
        .then((data) => {
          if (data.status === 'ok') {
            setPaletteResults(data.listings || [])
            setPaletteActive(0)
          }
        })
        .catch(() => {})
        .finally(() => setPaletteLoading(false))
    }, 200)
    return () => clearTimeout(t)
  }, [paletteQuery, paletteOpen])

  useEffect(() => {
    if (!paletteOpen) {
      setPaletteQuery('')
      setPaletteResults([])
      setPaletteActive(0)
    }
  }, [paletteOpen])

  function fetchPage(pageNum: number, append: boolean) {
    if (append) setLoadingMore(true)
    const params = new URLSearchParams({
      sort, page: String(pageNum), page_size: String(PAGE_SIZE),
      nsfw: nsfw ? '1' : '0',
    })
    if (tag) params.set('tag', tag)
    if (extraTags.length > 0) params.set('tags', extraTags.join(','))
    if (debouncedQuery) params.set('q', debouncedQuery)
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
  }, [sort, tag, nsfw, extraTags, debouncedQuery])

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

  async function copyInvite(guildId: string, inviteUrl: string) {
    try {
      await navigator.clipboard.writeText(inviteUrl)
      setCopiedId(guildId)
      setTimeout(() => setCopiedId((cur) => (cur === guildId ? null : cur)), 1500)
    } catch {
      window.prompt('Copy this invite link:', inviteUrl)
    }
  }

  function toggleExtraTag(t: string) {
    setExtraTags((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]))
  }

  const allTags = Array.from(new Set((listings || []).flatMap((l) => l.tags))).filter((t) => t !== 'nsfw').slice(0, 12)
  // Filtering is now done server-side (sort/tag/tags/nsfw/q all round-trip
  // through fetchPage), so what comes back is already the page to render.
  const filtered = listings || []
  const canLoadMore = (listings?.length || 0) < total
  const { user, signOut } = useSignedInUser()

  // Auto-load the next page when the sentinel at the bottom of the list
  // scrolls into view — replaces the old click-to-load-more button with
  // real infinite scroll. rootMargin fires the fetch a bit before the
  // sentinel is actually on-screen so it feels seamless.
  useEffect(() => {
    const el = sentinelRef.current
    if (!el || !canLoadMore) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !loadingMore) {
          const next = page + 1
          setPage(next)
          fetchPage(next, true)
        }
      },
      { rootMargin: '600px 0px' }
    )
    observer.observe(el)
    return () => observer.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canLoadMore, loadingMore, page, filtered.length, viewMode])

  // Recompute which slice of `filtered` is actually mounted, based on
  // scroll position relative to the list's top. Runs on scroll/resize and
  // whenever the data or view mode (which changes row height) changes.
  useEffect(() => {
    const rowsPerLine = viewMode === 'grid' ? GRID_COLS : 1
    const itemHeight = ITEM_HEIGHT[viewMode]
    const lineHeight = itemHeight // grid rows and list rows are both measured per-line
    function recompute() {
      const root = scrollRootRef.current
      if (!root) return
      const rect = root.getBoundingClientRect()
      const viewportTop = Math.max(0, -rect.top)
      const viewportHeight = window.innerHeight
      const buffer = lineHeight * 3
      const firstLine = Math.max(0, Math.floor((viewportTop - buffer) / lineHeight))
      const lastLine = Math.ceil((viewportTop + viewportHeight + buffer) / lineHeight)
      setViewportRange({
        start: firstLine * rowsPerLine,
        end: Math.min(filtered.length, (lastLine + 1) * rowsPerLine),
      })
    }
    recompute()
    window.addEventListener('scroll', recompute, { passive: true })
    window.addEventListener('resize', recompute)
    return () => {
      window.removeEventListener('scroll', recompute)
      window.removeEventListener('resize', recompute)
    }
  }, [filtered.length, viewMode])

  const rowsPerLine = viewMode === 'grid' ? GRID_COLS : 1
  const totalLines = Math.ceil(filtered.length / rowsPerLine)
  const startLine = Math.floor(viewportRange.start / rowsPerLine)
  const endLine = Math.ceil(viewportRange.end / rowsPerLine)
  const topSpacer = startLine * ITEM_HEIGHT[viewMode]
  const bottomSpacer = Math.max(0, (totalLines - endLine) * ITEM_HEIGHT[viewMode])
  const visibleListings = filtered.slice(viewportRange.start, viewportRange.end)

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
          {/* Distinct from "Add PRIME-BOT" above in intent — this one reads
              as "list your server here". PRIME-BOT is optional for listing
              now (see api/discord_login_oauth.py's bot_present flag), so
              this offers both paths instead of assuming one — see
              _AddServerButton.tsx. */}
          <AddServerButton className="pb-btn-secondary text-sm" />
        </div>
      </div>

      <div className="pb-filter-bar">
        <div className="flex items-center justify-between gap-2 mb-3">
          <button
            className="text-xs px-3 py-1.5 rounded-md flex items-center gap-1.5"
            style={{ background: 'var(--pb-surface-raised)', border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
            onClick={() => setPaletteOpen(true)}
          >
            <span>🔎 Quick jump</span>
            <kbd className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: 'rgba(255,255,255,0.08)' }}>⌘K</kbd>
          </button>
          <div className="flex items-center gap-0.5 rounded-md p-0.5" style={{ border: '1px solid var(--pb-line)' }}>
            <button
              className="text-xs px-2.5 py-1 rounded"
              style={viewMode === 'list' ? { background: 'var(--pb-accent)', color: '#fff' } : { color: 'var(--pb-text-faint)' }}
              onClick={() => setViewMode('list')}
              aria-pressed={viewMode === 'list'}
            >
              ☰ List
            </button>
            <button
              className="text-xs px-2.5 py-1 rounded"
              style={viewMode === 'grid' ? { background: 'var(--pb-accent)', color: '#fff' } : { color: 'var(--pb-text-faint)' }}
              onClick={() => setViewMode('grid')}
              aria-pressed={viewMode === 'grid'}
            >
              ▦ Grid
            </button>
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
          {extraTags.map((t) => (
            <button key={t} className="pb-chip" style={{ borderColor: 'var(--pb-accent)' }} onClick={() => toggleExtraTag(t)}>
              #{t}
              <span className="pb-chip-remove">×</span>
            </button>
          ))}
        </div>

        {allTags.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {/* Multi-select: click any number of these to AND them together
                (handled server-side via ?tags=a,b,c). The single `tag` from
                a category chip/URL stays a separate, always-included filter. */}
            {allTags.filter((t) => t !== tag).map((t) => (
              <button
                key={t}
                className="pb-chip"
                style={extraTags.includes(t) ? { borderColor: 'var(--pb-accent)', background: 'rgba(59,130,246,0.15)' } : undefined}
                onClick={() => toggleExtraTag(t)}
              >
                #{t}
              </button>
            ))}
          </div>
        )}
      </div>


      {error && <p className="text-sm" style={{ color: 'var(--pb-danger)' }}>{error}</p>}

      {!error && listings === null && (
        <div className={viewMode === 'grid' ? 'grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3' : 'space-y-3'}>
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="pb-skeleton-row" style={viewMode === 'grid' ? { height: ITEM_HEIGHT.grid - 12 } : undefined} />
          ))}
        </div>
      )}

      {!error && listings !== null && filtered.length === 0 && (
        <p className="text-sm" style={{ color: 'var(--pb-text-faint)' }}>No servers match yet — be the first!</p>
      )}

      {!error && filtered.length > 0 && (
        <div ref={scrollRootRef}>
          {/* Top spacer reserves the scroll height of rows above the mounted
              window so the scrollbar/scroll position stays correct even
              though only a slice of `filtered` is actually in the DOM. */}
          <div style={{ height: topSpacer }} aria-hidden />
          <ul className={viewMode === 'grid' ? 'grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3' : 'space-y-3'}>
          {visibleListings.map((l) => {
            const i = filtered.indexOf(l)
          const fire = FIRE_COLORS[i % FIRE_COLORS.length]
          const isHovered = hoveredId === l.guild_id
          // Hover/focus intensifies the existing glow rather than replacing
          // it with a different treatment — same fire palette, turned up.
          const glowStrength = isHovered ? 1.7 : 1
          // Top tier: manually or heuristically verified, or has any paid
          // boost active — gets the animated gradient border on top of the
          // normal fire glow, so it's visually distinct in the feed itself
          // and not just higher up thanks to sort order.
          const isTopTier = l.verified || l.boost_count > 0
          return (
            <li
              key={l.guild_id}
              className={`pb-listing-card rounded-lg border p-4 relative overflow-hidden${isTopTier ? ' pb-tier-glass' : ''}`}
              style={{
                borderColor: fire.hex,
                background: 'var(--pb-surface)',
                boxShadow: `0 0 0 1px rgba(${fire.rgb},${0.25 * glowStrength}), 0 0 ${20 * glowStrength}px rgba(${fire.rgb},${0.35 * glowStrength}), inset 0 0 30px rgba(${fire.rgb},0.06)`,
                // Stagger only the first page of cards in on load — beyond
                // that it'd just make "Load more" feel sluggish.
                ['--pb-delay' as string]: `${Math.min(i, 11) * 0.04}s`,
              }}
              onMouseEnter={() => setHoveredId(l.guild_id)}
              onMouseLeave={() => setHoveredId((cur) => (cur === l.guild_id ? null : cur))}
              onFocus={() => setHoveredId(l.guild_id)}
              onBlur={() => setHoveredId((cur) => (cur === l.guild_id ? null : cur))}
            >
              {l.banner_url && viewMode === 'list' && (
                // Faint banner wash behind the card content — stored on
                // every listing but never rendered before this.
                <div
                  aria-hidden
                  className="absolute inset-0 pointer-events-none"
                  style={{
                    backgroundImage: `linear-gradient(to bottom, rgba(20,21,25,0.55), var(--pb-surface) 85%), url(${l.banner_url})`,
                    backgroundSize: 'cover',
                    backgroundPosition: 'center',
                    opacity: 0.9,
                  }}
                />
              )}
              {l.banner_url && viewMode === 'grid' && (
                // Grid mode has room to show the banner as a real hero strip
                // rather than just a faint wash behind the text.
                <div
                  className="-mx-4 -mt-4 mb-3 rounded-t-lg"
                  style={{
                    height: 96,
                    backgroundImage: `url(${l.banner_url})`,
                    backgroundSize: 'cover',
                    backgroundPosition: 'center',
                  }}
                />
              )}
              <div className={viewMode === 'grid' ? 'flex flex-col gap-3 relative h-full' : 'flex items-start gap-3 relative'}>
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
                  <p className="text-xs mt-0.5 flex items-center gap-1.5 flex-wrap" style={{ color: 'var(--pb-text-faint)' }}>
                    <span>{l.member_count.toLocaleString()} members</span>
                    {l.member_count_trend && l.member_count_trend !== 'flat' && (
                      <span
                        title={`${l.member_count_delta! > 0 ? '+' : ''}${l.member_count_delta} since yesterday`}
                        style={{ color: l.member_count_trend === 'up' ? 'var(--pb-positive)' : 'var(--pb-danger)' }}
                      >
                        {l.member_count_trend === 'up' ? '↑' : '↓'}
                        {Math.abs(l.member_count_delta!).toLocaleString()}
                      </span>
                    )}
                    <span>· {l.vote_count} votes</span>
                    {l.online_count !== null && (
                      <span className="inline-flex items-center gap-1">
                        <span className="pb-online-dot" aria-hidden />
                        {l.online_count.toLocaleString()} online
                      </span>
                    )}
                  </p>
                  {l.description && (
                    <p className="text-sm mt-2" style={{ color: 'var(--pb-text-muted)' }}>
                      {l.description.length > 100 && expandedId !== l.guild_id
                        ? (
                          <>
                            {l.description.slice(0, 100)}…{' '}
                            <button
                              className="underline text-xs"
                              style={{ color: fire.hex }}
                              onClick={() => setExpandedId(l.guild_id)}
                            >
                              Read more
                            </button>
                          </>
                        )
                        : (
                          <>
                            {l.description}
                            {l.description.length > 100 && (
                              <button
                                className="underline text-xs ml-1"
                                style={{ color: fire.hex }}
                                onClick={() => setExpandedId(null)}
                              >
                                Show less
                              </button>
                            )}
                          </>
                        )}
                    </p>
                  )}
                  {l.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {l.tags.map((t) => (
                        <button key={t} className="pb-chip text-xs" onClick={() => setTag(t)}>#{t}</button>
                      ))}
                    </div>
                  )}
                </div>
                <div className={viewMode === 'grid' ? 'flex flex-wrap items-center gap-2 shrink-0 mt-auto' : 'flex flex-col items-end gap-2 shrink-0'}>
                  <a href={l.invite_url} target="_blank" rel="noopener noreferrer" className="pb-btn-secondary text-sm">
                    Join
                  </a>
                  <div className="flex items-center gap-1">
                    <button
                      className="text-xs px-2 py-1 rounded-md"
                      style={{ background: 'var(--pb-surface-raised)', border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
                      title="Copy invite link"
                      onClick={() => copyInvite(l.guild_id, l.invite_url)}
                    >
                      {copiedId === l.guild_id ? 'Copied ✓' : 'Copy'}
                    </button>
                    <button
                      className="text-xs px-2 py-1 rounded-md"
                      style={{ background: 'var(--pb-surface-raised)', border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
                      title="Show QR code"
                      onClick={() => setQrGuildId(qrGuildId === l.guild_id ? null : l.guild_id)}
                    >
                      QR
                    </button>
                  </div>
                  {qrGuildId === l.guild_id && (
                    // Uses a public QR-image API purely for the rendering
                    // math (no data leaves the client beyond the invite
                    // URL itself, which is already public) — no new
                    // dependency or backend endpoint needed for this.
                    <img
                      src={`https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=${encodeURIComponent(l.invite_url)}`}
                      alt={`QR code for ${l.guild_name} invite`}
                      className="w-24 h-24 rounded-md"
                      style={{ background: '#fff', padding: '4px' }}
                    />
                  )}
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
          {/* Bottom spacer, same purpose as the top one above. */}
          <div style={{ height: bottomSpacer }} aria-hidden />
        </div>
      )}

      {canLoadMore && (
        <div ref={sentinelRef} className="w-full mt-4 text-center text-xs" style={{ color: 'var(--pb-text-faint)' }}>
          {loadingMore ? 'Loading more…' : `${listings?.length || 0} of ${total} loaded — scroll for more`}
        </div>
      )}

      <section className="mt-10 pt-6 border-t text-sm" style={{ borderColor: 'var(--pb-line)', color: 'var(--pb-text-faint)' }}>
        Want your server listed? Run <code className="pb-code">/setup servers</code> in your Discord server
        (PRIME-BOT must already be a member) to get your private listing link.
        <br />
        Need help with something?{' '}
        <a href={SUPPORT_SERVER_INVITE} target="_blank" rel="noopener noreferrer" className="underline" style={{ color: 'var(--pb-accent)' }}>
          Contact support
        </a>
      </section>

      {paletteOpen && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center pt-24 px-4"
          style={{ background: 'rgba(5,6,10,0.7)' }}
          onClick={() => setPaletteOpen(false)}
        >
          <div
            className="w-full max-w-lg rounded-xl border overflow-hidden pb-cmdk-modal"
            style={{ background: 'var(--pb-surface)', borderColor: 'var(--pb-accent)' }}
            onClick={(e) => e.stopPropagation()}
          >
            <input
              autoFocus
              className="w-full px-4 py-3 text-sm outline-none"
              style={{ background: 'transparent', color: '#e5e7eb', borderBottom: '1px solid var(--pb-line)' }}
              placeholder="Jump to a server…"
              value={paletteQuery}
              onChange={(e) => setPaletteQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'ArrowDown') {
                  e.preventDefault()
                  setPaletteActive((cur) => Math.min(cur + 1, paletteResults.length - 1))
                } else if (e.key === 'ArrowUp') {
                  e.preventDefault()
                  setPaletteActive((cur) => Math.max(cur - 1, 0))
                } else if (e.key === 'Enter' && paletteResults[paletteActive]) {
                  window.location.href = `/servers/${paletteResults[paletteActive].guild_id}`
                }
              }}
            />
            <div className="max-h-80 overflow-y-auto">
              {paletteLoading && (
                <p className="px-4 py-3 text-xs" style={{ color: 'var(--pb-text-faint)' }}>Searching…</p>
              )}
              {!paletteLoading && paletteQuery.trim() && paletteResults.length === 0 && (
                <p className="px-4 py-3 text-xs" style={{ color: 'var(--pb-text-faint)' }}>No servers found.</p>
              )}
              {!paletteQuery.trim() && (
                <p className="px-4 py-3 text-xs" style={{ color: 'var(--pb-text-faint)' }}>
                  Start typing a server name, tag, or description — or press a chip below.
                </p>
              )}
              {paletteResults.map((r, i) => (
                <a
                  key={r.guild_id}
                  href={`/servers/${r.guild_id}`}
                  className="flex items-center gap-3 px-4 py-2.5 text-sm"
                  style={i === paletteActive ? { background: 'rgba(59,130,246,0.15)' } : undefined}
                  onMouseEnter={() => setPaletteActive(i)}
                >
                  {r.guild_icon_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={r.guild_icon_url} alt="" className="w-6 h-6 rounded-full shrink-0" />
                  ) : (
                    <div className="w-6 h-6 rounded-full shrink-0" style={{ background: 'var(--pb-surface-raised)' }} />
                  )}
                  <span className="flex-1 min-w-0 truncate">{r.guild_name}</span>
                  <span className="text-xs shrink-0" style={{ color: 'var(--pb-text-faint)' }}>
                    {r.member_count.toLocaleString()} members
                  </span>
                </a>
              ))}
            </div>
          </div>
        </div>
      )}

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
