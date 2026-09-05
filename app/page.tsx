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

// Neon smoke look, cycling one color every 3s with a soft crossfade between
// steps (transition on background-color, not the interval itself — the
// interval just swaps the target color, the browser tweens to it).
const SMOKE_COLORS = [
  '#3b82f6', // blue — matches the reference art
  '#a855f7', // purple
  '#ef4444', // red
  '#22d3ee', // cyan
  '#f97316', // orange
  '#ec4899', // pink
  '#22c55e', // green
]

export default function Page() {
  return (
    <main className="pb-page">
      <section className="relative overflow-hidden">
        <div className="absolute inset-0">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/hero-car.png"
            alt=""
            className="w-full h-full object-cover"
            style={{ objectPosition: '60% 50%' }}
          />
          <div
            className="absolute inset-0"
            style={{
              background:
                'linear-gradient(100deg, rgba(8,9,11,0.94) 0%, rgba(8,9,11,0.72) 32%, rgba(8,9,11,0.25) 58%, rgba(8,9,11,0.15) 100%)',
            }}
          />
          <TireSmoke />
        </div>

        <div className="relative mx-auto max-w-3xl px-6 pt-10 pb-24 sm:pt-14 sm:pb-32">
          <Suspense fallback={null}>
            <ListingBanner />
          </Suspense>

          <div className="mt-16 sm:mt-20 max-w-xl">
            <p className="text-sm" style={{ color: 'var(--pb-accent)' }}>
              PRIME-BOT
            </p>
            <h1 className="pb-heading mt-3 text-4xl sm:text-5xl font-semibold leading-[1.1]">
              One bot for the parts of running a server nobody enjoys doing by hand.
            </h1>
            <p className="mt-5 text-base sm:text-lg" style={{ color: 'var(--pb-text-muted)' }}>
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

          <div className="mt-14 sm:mt-0 sm:absolute sm:bottom-10 sm:right-6 sm:w-[280px]">
            <ActivityFeed />
          </div>
        </div>
      </section>

      <div className="mx-auto max-w-3xl px-6">
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

// Two smoke sources anchored (by percentage, so they track the image at any
// hero width) roughly over the car's front and rear tires in hero-car.png.
// Each is a stack of blurred, screen-blended circles that drift and pulse —
// the blur is what turns a plain circle into a soft, gradient-like glow, so
// no actual gradient gets animated (gradients don't interpolate color
// reliably across browsers; solid background-color does, cleanly, via a
// plain CSS transition).
function TireSmoke() {
  const [colorIndex, setColorIndex] = useState(0)
  const [reduced, setReduced] = useState(false)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduced(mq.matches)
  }, [])

  useEffect(() => {
    const id = setInterval(() => {
      setColorIndex((i) => (i + 1) % SMOKE_COLORS.length)
    }, 3000)
    return () => clearInterval(id)
  }, [])

  const color = SMOKE_COLORS[colorIndex]

  return (
    <>
      <SmokePlume left="45%" top="66%" scale={0.75} color={color} reduced={reduced} delay={0} />
      <SmokePlume left="87%" top="62%" scale={1.15} color={color} reduced={reduced} delay={1.4} />
    </>
  )
}

// A wheel effect made of two layers:
// - WheelBlur: a fast-spinning conic-gradient ring right over the tire, so
//   the rim itself reads as spinning (a real photo can't rotate, but a
//   blurred spinning ring sitting on top of it sells the motion the same
//   way a motion-blur photo does).
// - Several SmokeStreak particles that launch from the tire and travel
//   backward/outward with rotation and fade, instead of one soft pulsing
//   blob — this is what actually reads as "smoking out" rather than a
//   glow. Direction is mirrored per wheel (front tire kicks smoke back
//   toward the rear, rear tire kicks it further back and out).
function SmokePlume({
  left, top, scale, color, reduced, delay,
}: {
  left: string; top: string; scale: number; color: string; reduced: boolean; delay: number
}) {
  const wheelSize = 90 * scale
  const streaks = [0, 1, 2, 3]

  return (
    <div className="absolute" style={{ left, top, width: 0, height: 0 }} aria-hidden="true">
      <div
        style={{
          position: 'absolute',
          left: -wheelSize / 2,
          top: -wheelSize / 2,
          width: wheelSize,
          height: wheelSize,
          borderRadius: '9999px',
          background: `conic-gradient(from 0deg, transparent 0deg, ${color} 60deg, transparent 140deg, transparent 220deg, ${color} 280deg, transparent 360deg)`,
          filter: `blur(${5 * scale}px)`,
          opacity: 0.65,
          mixBlendMode: 'screen',
          transition: 'background 1.4s ease',
          animation: reduced ? 'none' : 'pb-wheel-spin 0.5s linear infinite',
        }}
      />
      {streaks.map((i) => (
        <div
          key={i}
          style={{
            position: 'absolute',
            left: -30 * scale,
            top: -18 * scale,
            width: (70 + i * 22) * scale,
            height: (34 + i * 10) * scale,
            borderRadius: '9999px',
            backgroundColor: color,
            filter: `blur(${(14 + i * 4) * scale}px)`,
            opacity: 0.5,
            mixBlendMode: 'screen',
            transition: 'background-color 1.4s ease',
            animation: reduced
              ? 'none'
              : `pb-smoke-streak-${i % 2 === 0 ? 'a' : 'b'} ${3.2 + i * 0.6}s ease-out ${delay + i * 0.5}s infinite`,
          }}
        />
      ))}
    </div>
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
