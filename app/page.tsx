// path: app/page.tsx

// Server-listing directory removed from this site (see git history for
// the old ServerDirectory/_ServerDirectory-based version). What's left:
// hero + links into the two flows that remain — /unlock and the
// dashboard sign-in (api/discord_login_oauth.py).

import Image from 'next/image'

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

export default function Page() {
  return (
    <main className="pb-page px-6 py-12">
      <Hero />

      <div className="max-w-2xl mx-auto">
        <section className="flex flex-wrap gap-4">
          <a href="/unlock" className="pb-btn-primary">
            Unlock
          </a>
          <a href={`${API_BASE}/api/discord_login_oauth`} className="pb-btn-secondary">
            Sign in with Discord
          </a>
        </section>
      </div>
    </main>
  )
}

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
          <span className="text-[10px] font-bold tracking-widest uppercase" style={{ color: 'var(--pb-accent)' }}>
            ⚔ PRIME BOT
          </span>
        </div>

        <h1
          className="text-3xl sm:text-4xl font-bold leading-tight tracking-tight"
          style={{
            color: 'var(--pb-accent)',
            textShadow: '0 0 24px rgba(59,130,246,0.55), 0 2px 12px rgba(0,0,0,0.8)',
          }}
        >
          PRIME BOT
        </h1>
        <p
          className="mt-2 text-sm sm:text-base max-w-md"
          style={{ color: 'rgba(229,231,235,0.85)', textShadow: '0 1px 6px rgba(0,0,0,0.9)' }}
        >
          Moderation, leveling, and community management for your Discord server.
        </p>
      </div>
    </div>
  )
}
