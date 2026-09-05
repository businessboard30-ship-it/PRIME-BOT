// path: app/login/servers/page.tsx

'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'

type ManagedGuild = {
  guild_id: string
  guild_name: string
  guild_icon_url: string | null
  token: string
  clone_id: number | null
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
  const session = searchParams.get('session') || ''
  const urlError = searchParams.get('error') || ''

  const [guilds, setGuilds] = useState<ManagedGuild[] | null>(null)
  const [error, setError] = useState<string | null>(urlError || null)
  const [loading, setLoading] = useState(!urlError)

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
          setGuilds(data.guilds)
        }
      })
      .catch(() => setError('Network error loading your servers'))
      .finally(() => setLoading(false))
  }, [session, urlError])

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
        <h1 className="pb-heading text-2xl font-semibold mb-2">No servers to manage yet</h1>
        <p className="text-sm max-w-md" style={{ color: 'var(--pb-text-muted)' }}>
          Either PRIME-BOT isn't in any server you have Manage Server on, or it hasn't been added
          anywhere yet.
        </p>
        <a href={BOT_INVITE_URL} target="_blank" rel="noopener noreferrer" className="pb-btn-primary inline-flex mt-6">
          + Add PRIME-BOT to a server
        </a>
      </Shell>
    )
  }

  return (
    <Shell>
      <h1 className="pb-heading text-2xl font-semibold mb-1">Choose a server</h1>
      <p className="text-sm mb-8" style={{ color: 'var(--pb-text-faint)' }}>
        Servers you manage that already have PRIME-BOT.
      </p>

      <div className="rounded-lg border" style={{ borderColor: 'var(--pb-line)' }}>
        {guilds.map((g, i) => (
          <a
            key={g.guild_id}
            href={`/dashboard/${g.guild_id}?token=${encodeURIComponent(g.token)}${g.clone_id != null ? `&clone_id=${g.clone_id}` : ''}`}
            className="flex items-center gap-3 px-4 py-3 transition-colors hover:opacity-80"
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
            <span className="font-medium flex-1 truncate">{g.guild_name}</span>
            <span className="text-sm" style={{ color: 'var(--pb-accent)' }}>Open dashboard →</span>
          </a>
        ))}
      </div>
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="pb-page px-6 py-12">
      <div className="max-w-lg mx-auto">{children}</div>
    </main>
  )
}
