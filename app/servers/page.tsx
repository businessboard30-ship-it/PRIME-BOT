// path: app/servers/page.tsx

'use client'

import { useEffect, useState } from 'react'

type Listing = {
  guild_id: string
  clone_id: number | null
  guild_name: string
  guild_icon_url: string | null
  member_count: number
  invite_url: string
  description: string
  tags: string[]
  ref_code: string | null
  vote_count: number
  confirmed_conversions: number
}

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const DISCORD_CLIENT_ID = process.env.NEXT_PUBLIC_DISCORD_CLIENT_ID || ''
// Administrator (bit 8) — this bot's automod, roles, channels, and voice
// features span enough of Discord's permission surface that a hand-picked
// subset risks silently breaking something. Narrow this later once every
// permission this bot actually needs has been audited.
const BOT_INVITE_URL = DISCORD_CLIENT_ID
  ? `https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&scope=bot+applications.commands&permissions=8`
  : ''
const SUPPORT_SERVER_INVITE = 'https://discord.gg/DYfajXrP9B'

export default function ServersPage() {
  const [listings, setListings] = useState<Listing[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [voteBanner, setVoteBanner] = useState<{ ok: boolean; msg: string } | null>(null)

  useEffect(() => {
    fetch(`${API_BASE}/api/server_listings`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== 'ok') {
          setError(data.message || 'Could not load the directory')
        } else {
          setListings(data.listings)
        }
      })
      .catch(() => setError('Network error loading the directory'))

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

  function voteHref(l: Listing) {
    const qs = new URLSearchParams({ guild_id: l.guild_id })
    if (l.clone_id != null) qs.set('clone_id', String(l.clone_id))
    return `${API_BASE}/api/server_listing_vote_oauth?${qs.toString()}`
  }

  const filtered = (listings || []).filter((l) => {
    if (!query.trim()) return true
    const q = query.trim().toLowerCase()
    return (
      l.guild_name.toLowerCase().includes(q) ||
      l.description.toLowerCase().includes(q) ||
      l.tags.some((t) => t.toLowerCase().includes(q))
    )
  })

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100 px-6 py-12">
      <div className="max-w-3xl mx-auto">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Server directory</h1>
            <p className="text-sm text-neutral-400 mt-2 max-w-lg">
              Public servers running PRIME-BOT. Listings go live instantly — see{' '}
              <span className="text-neutral-300">List your server</span> below.
            </p>
          </div>
          <a href="#list-your-server" className="btn-primary shrink-0">
            + List your server
          </a>
        </div>

        {voteBanner && (
          <p className={`text-sm mt-6 ${voteBanner.ok ? 'text-emerald-400' : 'text-red-400'}`}>
            {voteBanner.msg}
          </p>
        )}

        <input
          className="input mt-8 w-full max-w-sm"
          placeholder="Search servers, tags…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />

        <div className="mt-8 grid gap-4 sm:grid-cols-2">
          {listings === null && !error && (
            <p className="text-sm text-neutral-500 col-span-full">Loading…</p>
          )}
          {error && <p className="text-sm text-red-400 col-span-full">{error}</p>}
          {listings !== null && filtered.length === 0 && (
            <p className="text-sm text-neutral-500 col-span-full">
              {query ? `No servers match "${query}".` : 'No servers listed yet — be the first!'}
            </p>
          )}
          {filtered.map((l) => (
            <ServerCard key={l.guild_id} listing={l} voteHref={voteHref(l)} />
          ))}
        </div>

        <section id="list-your-server" className="mt-16 pt-8 border-t border-neutral-800">
          <h2 className="text-lg font-semibold">List your server</h2>
          <p className="text-sm text-neutral-400 mt-2 max-w-lg">
            There's no open submission form here on purpose — a listing can only be created by
            an admin of a server the bot is already in, so every listing is automatically
            verified with no waiting on approval.
          </p>

          <div className="mt-4 p-4 rounded-lg border border-neutral-800 bg-neutral-900/40 max-w-lg">
            <p className="text-sm text-neutral-300">Don't have PRIME-BOT in your server yet?</p>
            {BOT_INVITE_URL ? (
              <a href={BOT_INVITE_URL} target="_blank" rel="noopener noreferrer" className="btn-primary inline-block mt-2">
                + Add PRIME-BOT to your server
              </a>
            ) : (
              <p className="text-xs text-neutral-500 mt-1">
                Ask in the{' '}
                <a href={SUPPORT_SERVER_INVITE} target="_blank" rel="noopener noreferrer" className="underline">
                  support server
                </a>{' '}
                for an invite link.
              </p>
            )}
            <p className="text-xs text-neutral-500 mt-2">
              Already added? Continue with the steps below.
            </p>
          </div>

          <ol className="mt-4 space-y-2 text-sm text-neutral-300 list-decimal list-inside">
            <li>
              In your Discord server, run <code className="code">/setup servers</code> (you'll need the{' '}
              <span className="text-neutral-100">Manage Server</span> permission).
            </li>
            <li>The bot replies with a private link — open it.</li>
            <li>Add your invite link, a short description, and up to 5 tags, then submit.</li>
          </ol>
          <p className="text-xs text-neutral-500 mt-4">
            Already listed? Rerunning <code className="code">/setup servers</code> gives you the same
            link back, so you can edit your listing any time.
          </p>
        </section>
      </div>

      <style jsx global>{`
        .input {
          background: #171717;
          border: 1px solid #333;
          border-radius: 6px;
          padding: 8px 12px;
          font-size: 14px;
          color: inherit;
        }
        .code {
          background: #171717;
          border-radius: 4px;
          padding: 1px 6px;
          font-size: 12px;
        }
        .btn-primary {
          display: inline-flex;
          align-items: center;
          background: #2563eb;
          border-radius: 8px;
          padding: 8px 16px;
          font-size: 14px;
          font-weight: 500;
          color: white;
          text-decoration: none;
        }
        .btn-primary:hover {
          background: #1d4ed8;
        }
      `}</style>
    </main>
  )
}

function ServerCard({ listing, voteHref }: { listing: Listing; voteHref: string }) {
  const [copied, setCopied] = useState(false)
  const boosted = listing.confirmed_conversions > 0

  function copyRefLink() {
    if (!listing.ref_code) return
    const url = `${window.location.origin}/servers?ref=${listing.ref_code}`
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4 hover:border-neutral-700 transition-colors">
      <div className="flex items-center gap-3">
        {listing.guild_icon_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={listing.guild_icon_url} alt="" className="w-10 h-10 rounded-full" />
        ) : (
          <div className="w-10 h-10 rounded-full bg-neutral-800 flex items-center justify-center text-sm text-neutral-500">
            {listing.guild_name.slice(0, 1).toUpperCase()}
          </div>
        )}
        <div className="min-w-0 flex-1">
          <p className="font-medium truncate">{listing.guild_name}</p>
          <p className="text-xs text-neutral-500">{listing.member_count.toLocaleString()} members</p>
        </div>
        {boosted && (
          <span className="text-[11px] shrink-0 bg-emerald-900/40 text-emerald-400 rounded px-2 py-0.5">
            Boosted
          </span>
        )}
      </div>
      {listing.description && (
        <p className="text-sm text-neutral-400 mt-3 line-clamp-2">{listing.description}</p>
      )}
      {listing.tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-3">
          {listing.tags.map((t) => (
            <span key={t} className="text-xs bg-neutral-800 rounded-full px-2 py-0.5 text-neutral-400">
              #{t}
            </span>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 mt-4 pt-3 border-t border-neutral-800">
        <a
          href={voteHref}
          className="flex-1 inline-flex items-center justify-center gap-1.5 text-sm rounded-md border border-neutral-700 py-1.5 hover:bg-neutral-800 transition-colors"
        >
          ▲ {listing.vote_count}
        </a>
        <a
          href={listing.invite_url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex-1 inline-flex items-center justify-center gap-1.5 text-sm rounded-md bg-blue-600 hover:bg-blue-700 py-1.5 text-white transition-colors"
        >
          Join
        </a>
        {listing.ref_code && (
          <button
            onClick={copyRefLink}
            title="Copy your referral link — joins through it boost this listing's ranking"
            className="shrink-0 text-sm rounded-md border border-neutral-700 px-2.5 py-1.5 hover:bg-neutral-800 transition-colors text-neutral-400"
          >
            {copied ? '✓' : '🔗'}
          </button>
        )}
      </div>
    </div>
  )
}
