// path: app/login/servers/page.tsx

'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams, useRouter } from 'next/navigation'
import Image from 'next/image'
import { rememberLoginSession, forgetLoginSession } from '../../_signedInSession'

type ManagedGuild = {
  guild_id: string
  guild_name: string
  guild_icon_url: string | null
  token: string | null
  listing_token: string
  clone_id: number | null
  bot_present: boolean
}

type SignedInUser = {
  id: string
  username: string
  avatar_url: string
}

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const DISCORD_CLIENT_ID = process.env.NEXT_PUBLIC_DISCORD_CLIENT_ID || ''
const BOT_INVITE_URL = DISCORD_CLIENT_ID
  ? `https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&scope=bot+applications.commands&permissions=8`
  : 'https://discord.gg/DYfajXrP9B'

// Same reason as app/servers/submit/page.tsx: useSearchParams() needs a
// Suspense boundary or static prerendering fails the build.
export default function LoginServersPage() {
  return (
    <Suspense fallback={<Shell><p className="text-sm" style={{ color: 'var(--pb-text-muted)' }}>Loading…</p></Shell>}>
      <LoginServersPageInner />
    </Suspense>
  )
}

function LoginServersPageInner() {
  const searchParams = useSearchParams()
  const router = useRouter()
  const session = searchParams.get('session') || ''
  const urlError = searchParams.get('error') || ''

  const [user, setUser] = useState<SignedInUser | null>(null)
  const [guilds, setGuilds] = useState<ManagedGuild[] | null>(null)
  const [error, setError] = useState<string | null>(urlError || null)
  const [loading, setLoading] = useState(!urlError)
  const [signingOut, setSigningOut] = useState(false)

  useEffect(() => {
    if (urlError) return
    if (!session) {
      setError('Missing sign-in session — try signing in again.')
      setLoading(false)
      return
    }
    fetch(`${API_BASE}/api/discord_login_oauth?session=${encodeURIComponent(session)}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== 'ok') {
          setError(data.message || 'Could not load your servers')
        } else {
          setUser(data.user || null)
          setGuilds(data.guilds)
          // Makes the sign-in visible on every other page too (directory
          // header, homepage) — see _signedInSession.ts.
          rememberLoginSession(session)
        }
      })
      .catch(() => setError('Network error loading your servers'))
      .finally(() => setLoading(false))
  }, [session, urlError])

  async function signOut() {
    if (!session || signingOut) return
    setSigningOut(true)
    try {
      await fetch(`${API_BASE}/api/discord_login_oauth?session=${encodeURIComponent(session)}`, {
        method: 'DELETE',
      })
    } catch {
      // Session is a short-lived, low-value credential (see discord_login_oauth.py) —
      // even if the DELETE didn't land, sending the user home with a dead session id
      // in the URL is a safe fallback, not a security gap.
    } finally {
      forgetLoginSession()
      router.push('/')
    }
  }

  const header = user ? (
    <div
      className="relative z-10 flex items-center gap-3 mb-8 rounded-lg p-3"
      style={{ background: 'var(--pb-surface)', border: '1px solid var(--pb-line)' }}
    >
      <img
        src={user.avatar_url}
        alt=""
        className="w-9 h-9 rounded-full"
        style={{ border: '1px solid var(--pb-line)' }}
      />
      <span className="text-sm font-medium flex-1 truncate">{user.username}</span>
      <button
        onClick={signOut}
        disabled={signingOut}
        className="text-sm px-3 py-1.5 rounded-md font-medium disabled:opacity-50"
        style={{ border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
      >
        {signingOut ? 'Signing out…' : 'Sign out'}
      </button>
    </div>
  ) : null

  if (loading) {
    return <Shell><p className="text-sm" style={{ color: 'var(--pb-text-muted)' }}>Checking your servers…</p></Shell>
  }

  if (error) {
    return (
      <Shell>
        <p className="text-sm" style={{ color: 'var(--pb-danger)' }}>{error}</p>
        <a href={`${API_BASE}/api/discord_login_oauth`} className="pb-btn-primary inline-flex mt-4">
          Sign in with Discord
        </a>
      </Shell>
    )
  }

  if (!guilds || guilds.length === 0) {
    return (
      <Shell>
        {header}
        <h1 className="pb-heading text-2xl font-semibold mb-2">No servers to manage yet</h1>
        <p className="text-sm max-w-md" style={{ color: 'var(--pb-text-muted)' }}>
          You need Manage Server (or to be the owner) on a Discord server to list it here —
          PRIME-BOT doesn't need to be in it.
        </p>
        <a href={BOT_INVITE_URL} target="_blank" rel="noopener noreferrer" className="pb-btn-primary inline-flex mt-6">
          + Add PRIME-BOT to a server
        </a>
      </Shell>
    )
  }

  return (
    <Shell>
      {header}
      <h1 className="pb-heading text-2xl font-semibold mb-1">Choose a server</h1>
      <p className="text-sm mb-8" style={{ color: 'var(--pb-text-faint)' }}>
        Servers you manage. PRIME-BOT doesn't need to be added to list one — servers with the bot
        already in them also get a dashboard link and a live member count.
      </p>

      <div className="rounded-lg border" style={{ borderColor: 'var(--pb-line)' }}>
        {guilds.map((g, i) => (
          <div
            key={g.guild_id}
            className="flex items-center gap-3 px-4 py-3"
            style={{ borderBottom: i === guilds.length - 1 ? 'none' : '1px solid var(--pb-line)' }}
          >
            {g.guild_icon_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={g.guild_icon_url} alt="" className="w-9 h-9 rounded-full" />
            ) : (
              <div
                className="w-9 h-9 rounded-full flex items-center justify-center text-sm"
                style={{ background: 'var(--pb-surface-raised)', color: 'var(--pb-text-faint)' }}
              >
                {g.guild_name.slice(0, 1).toUpperCase()}
              </div>
            )}
            <div className="flex-1 min-w-0">
              <span className="font-medium truncate block">{g.guild_name}</span>
              {!g.bot_present && (
                <span className="text-xs" style={{ color: 'var(--pb-text-faint)' }}>
                  PRIME-BOT not added — listing still works, just no dashboard or live member count yet
                </span>
              )}
            </div>
            <a
              href={`/servers/submit?token=${encodeURIComponent(g.listing_token)}&guild_id=${g.guild_id}`}
              className="text-sm px-3 py-1.5 rounded-md font-medium shrink-0"
              style={{ border: '1px solid var(--pb-accent)', color: 'var(--pb-accent)' }}
            >
              List this server
            </a>
            {g.bot_present && g.token ? (
              <a
                href={`/dashboard/${g.guild_id}?token=${encodeURIComponent(g.token)}${g.clone_id != null ? `&clone_id=${g.clone_id}` : ''}`}
                className="text-sm shrink-0"
                style={{ color: 'var(--pb-text-faint)' }}
              >
                Open dashboard →
              </a>
            ) : (
              <a
                href={BOT_INVITE_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm shrink-0"
                style={{ color: 'var(--pb-accent)' }}
              >
                + Add bot
              </a>
            )}
          </div>
        ))}
      </div>
    </Shell>
  )
}

// Same full-bleed background-hero treatment as /servers/submit — this page
// is reached right after the Discord OAuth redirect, so it shouldn't feel
// like a bare utility screen.
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="relative px-6 py-12 min-h-screen overflow-hidden">
      <Image
        src="/hero-car.png"
        alt=""
        fill
        priority
        sizes="100vw"
        className="object-cover -z-20"
        style={{ objectPosition: '50% center' }}
      />
      <div
        className="fixed inset-0 -z-10"
        style={{ background: 'linear-gradient(180deg, rgba(5,6,10,0.55) 0%, rgba(5,6,10,0.82) 35%, rgba(5,6,10,0.94) 100%)' }}
      />
      {/* isolate: pins this page's content to its own stacking context so
          nothing from an in-flight route transition (e.g. the homepage's
          ListingBanner) can ever composite on top of it again. */}
      <div className="relative max-w-lg mx-auto isolate">{children}</div>
    </main>
  )
}
