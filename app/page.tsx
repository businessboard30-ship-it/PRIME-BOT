'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'

const DISCORD_CLIENT_ID = process.env.NEXT_PUBLIC_DISCORD_CLIENT_ID || ''
const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const BOT_INVITE_URL = DISCORD_CLIENT_ID
  ? `https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&scope=bot+applications.commands&permissions=8`
  : 'https://discord.gg/DYfajXrP9B'
const LOGIN_URL = `${API_BASE}/api/discord_login_oauth`

const FEATURES = [
  {
    name: 'Auto-moderation',
    detail: 'Word filters, invite links, mass mentions, and flood spam — caught before your mods have to step in.',
  },
  {
    name: 'Leveling',
    detail: 'Members earn XP from activity and unlock roles automatically as they level up.',
  },
  {
    name: 'Invite tracking',
    detail: "See exactly which invite brought each member in, and who's bringing the most people.",
  },
  {
    name: 'Welcome messages',
    detail: 'A DM and an in-server greeting, both editable, sent the moment someone joins.',
  },
  {
    name: 'Reaction roles',
    detail: 'Self-serve role panels members set up themselves by reacting.',
  },
  {
    name: 'Server directory',
    detail: 'List your server publicly and get discovered by people looking for a community like yours.',
  },
]

// Each line pairs with the feature it's demonstrating, in the same order as
// FEATURES, so the two stay visually linked as the feed cycles.
const ACTIVITY_LINES = [
  { kind: 'mod', text: 'Message removed — blocked word detected', meta: '#general' },
  { kind: 'level', text: 'sana leveled up to Level 12 — role @Regular granted', meta: '' },
  { kind: 'invite', text: 'kwame_ joined via invite from Yaw — 47 total invites', meta: '' },
  { kind: 'welcome', text: 'Welcome message sent to new member', meta: '#welcome' },
  { kind: 'role', text: 'obed reacted \u2192 role @Gamer added', meta: '' },
]

export default function Page() {
  return (
    <main className="pb-page">
      <div className="mx-auto max-w-3xl px-6">
        <Suspense fallback={null}>
          <ListingBanner />
        </Suspense>
        <section className="pt-24 pb-16 sm:pt-32 sm:pb-20">
          <div className="grid gap-10 sm:grid-cols-[1.1fr_0.9fr] sm:items-center">
            <div>
              <p className="text-sm" style={{ color: 'var(--pb-accent)' }}>
                PRIME-BOT
              </p>
              <h1 className="pb-heading mt-3 text-4xl sm:text-5xl font-semibold leading-[1.1]">
                One bot for the parts of running a server nobody enjoys doing by hand.
              </h1>
              <p className="mt-5 text-base sm:text-lg max-w-xl" style={{ color: 'var(--pb-text-muted)' }}>
                Moderation, leveling, invites, and welcomes — set up in a few commands, running
                quietly in the background after that.
              </p>
              <div className="mt-8 flex flex-wrap items-center gap-3">
                <a href={BOT_INVITE_URL} target="_blank" rel="noopener noreferrer" className="pb-btn-primary">
                  Add to Discord
                </a>
                <a href={LOGIN_URL} className="pb-btn-secondary">
                  Sign in with Discord
                </a>
              </div>
              <p className="mt-3 text-xs" style={{ color: 'var(--pb-text-faint)' }}>
                Already added it somewhere? Sign in to manage a server you admin.
              </p>
            </div>

            <ActivityFeed />
          </div>
        </section>

        <section className="pb-16 border-t" style={{ borderColor: 'var(--pb-line)' }}>
          <div className="pt-16 grid gap-x-8 gap-y-10 sm:grid-cols-2">
            {FEATURES.map((f) => (
              <div key={f.name}>
                <h2 className="pb-heading text-base font-medium">{f.name}</h2>
                <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--pb-text-muted)' }}>
                  {f.detail}
                </p>
              </div>
            ))}
          </div>
        </section>

        <section className="pb-24 border-t" style={{ borderColor: 'var(--pb-line)' }}>
          <div className="pt-16 flex flex-wrap items-center justify-between gap-4">
            <div>
              <h2 className="pb-heading text-xl font-medium">Already have PRIME-BOT?</h2>
              <p className="mt-2 text-sm" style={{ color: 'var(--pb-text-muted)' }}>
                Run <code className="pb-code">/setup servers</code> in your server to list it, or{' '}
                <code className="pb-code">/automod dashboard</code> to open your settings — or
                just sign in above.
              </p>
            </div>
            <a href="/servers" className="pb-btn-primary shrink-0">
              Go to the directory
            </a>
          </div>
        </section>
      </div>
    </main>
  )
}

// Shown when /setup servers sent someone here with listing params attached
// (see discord_bot/cogs/server_listing.py) — lets them see the landing page
// first, then continue on to the actual submit form at /servers/submit
// rather than dropping them straight into a form with no context for what
// PRIME-BOT even is.
function ListingBanner() {
  const params = useSearchParams()
  const guildId = params.get('guild_id')
  const token = params.get('token')
  if (!guildId || !token) return null

  const submitUrl = `/servers/submit?${params.toString()}`

  return (
    <div
      className="mt-6 flex flex-wrap items-center justify-between gap-4 rounded-lg border p-4"
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

// One deliberate hero moment, per the brief: a live-looking feed of the
// bot's actual event types, cycling one at a time. Respects
// prefers-reduced-motion by freezing on the first line instead of
// disabling the visual outright, so the "what it does" content still reads.
function ActivityFeed() {
  const [index, setIndex] = useState(0)
  const [reduced, setReduced] = useState(false)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduced(mq.matches)
    if (mq.matches) return
    const id = setInterval(() => {
      setIndex((i) => (i + 1) % ACTIVITY_LINES.length)
    }, 2400)
    return () => clearInterval(id)
  }, [])

  const visible = reduced ? ACTIVITY_LINES.slice(0, 1) : ACTIVITY_LINES

  return (
    <div
      className="rounded-lg border p-4 sm:p-5"
      style={{ borderColor: 'var(--pb-line)', background: 'var(--pb-surface)' }}
      aria-hidden="true"
    >
      <div className="flex items-center gap-1.5 mb-4">
        <span className="w-2 h-2 rounded-full" style={{ background: 'var(--pb-danger)' }} />
        <span className="w-2 h-2 rounded-full" style={{ background: '#f6c343' }} />
        <span className="w-2 h-2 rounded-full" style={{ background: 'var(--pb-positive)' }} />
        <span className="ml-2 text-xs" style={{ color: 'var(--pb-text-faint)' }}>
          server activity
        </span>
      </div>
      <div className="h-24 relative overflow-hidden">
        {visible.map((line, i) => (
          <div
            key={line.text}
            className="absolute inset-x-0 top-0 flex items-start gap-2 text-sm"
            style={{
              opacity: i === index % visible.length ? 1 : 0,
              transform: i === index % visible.length ? 'translateY(0)' : 'translateY(4px)',
              transition: 'opacity 0.4s ease, transform 0.4s ease',
            }}
          >
            <ActivityDot kind={line.kind} />
            <div className="min-w-0">
              <p className="truncate" style={{ color: 'var(--pb-text)' }}>
                {line.text}
              </p>
              {line.meta && (
                <p className="text-xs mt-0.5" style={{ color: 'var(--pb-text-faint)' }}>
                  {line.meta}
                </p>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-1.5 mt-2">
        {visible.map((line, i) => (
          <span
            key={line.text}
            className="h-1 flex-1 rounded-full"
            style={{
              background: i === index % visible.length ? 'var(--pb-accent)' : 'var(--pb-line)',
              transition: 'background-color 0.3s ease',
            }}
          />
        ))}
      </div>
    </div>
  )
}

function ActivityDot({ kind }: { kind: string }) {
  const color =
    kind === 'mod' ? 'var(--pb-danger)' :
    kind === 'level' ? '#f6c343' :
    kind === 'invite' ? 'var(--pb-accent)' :
    kind === 'welcome' ? 'var(--pb-positive)' :
    'var(--pb-accent)'
  return (
    <span
      className="w-1.5 h-1.5 rounded-full mt-1.5 shrink-0"
      style={{ background: color }}
    />
  )
}
