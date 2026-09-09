// path: app/servers/_BoostModal.tsx

'use client'

/**
 * Boost purchase modal, opened from the "Boost" button on a listing card
 * in _ServerDirectory.tsx (replacing the old inline "Copy boost link"
 * button — that link now lives here instead, alongside the boost tiers).
 *
 * Payment is wired through Paystack: picking a tier and hitting "Confirm
 * boost" calls api/apply_boost.py, which now starts a real Paystack
 * transaction and returns a checkout link instead of crediting boosts
 * directly. This redirects the browser there; the boost itself is
 * credited server-side once api/paystack_webhook.py sees the charge
 * succeed (payment_type 'listing_boost'), same deferred-apply pattern as
 * every other paid feature in this codebase — there's no page to bounce
 * back to here, so the listing just shows the new boost_count next time
 * it's loaded.
 */

import { useState } from 'react'

const PRICE_PER_BOOST = 0.12

type Tier = { boosts: number; price: number; label: string; popular?: boolean }

const TIERS: Tier[] = [
  { boosts: 10, price: 1.2, label: '10 boosts' },
  { boosts: 20, price: 2.4, label: '20 boosts', popular: true },
  { boosts: 50, price: 6.0, label: '50 boosts' },
]

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://prime-bot.example.com'

export default function BoostModal({
  guildName,
  guildId,
  cloneId,
  refCode,
  onClose,
  onBoosted,
}: {
  guildName: string
  guildId: string
  cloneId: number | null
  refCode: string | null
  onClose: () => void
  onBoosted: (newBoostCount: number) => void
}) {
  const [selected, setSelected] = useState<number>(20)
  const [customBoosts, setCustomBoosts] = useState<string>('')
  const [email, setEmail] = useState<string>('')
  const [status, setStatus] = useState<'idle' | 'applying' | 'redirecting' | 'error'>('idle')
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const isCustom = selected === -1
  const customAmount = Math.max(0, parseInt(customBoosts || '0', 10) || 0)
  const amount = isCustom ? customAmount : selected
  const price = isCustom ? customAmount * PRICE_PER_BOOST : (TIERS.find((t) => t.boosts === selected)?.price ?? 0)
  const emailValid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())

  async function confirmBoost() {
    if (amount <= 0) {
      setErrorMsg('Choose a boost amount first')
      return
    }
    if (!emailValid) {
      setErrorMsg('Enter a valid email for the receipt')
      return
    }
    setErrorMsg(null)
    setStatus('applying')
    const url = `${API_BASE}/api/apply_boost`
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ guild_id: guildId, clone_id: cloneId, amount, email: email.trim() }),
      })
      let data: any = null
      try {
        data = await res.json()
      } catch {
        // Response wasn't JSON at all — the request reached *something*
        // (so this isn't a true network failure), but not our API
        // returning a clean error body. Surface the HTTP status instead
        // of the generic network message so this is diagnosable: usually
        // means API_BASE is pointed at the wrong deployment, or the
        // /api/apply_boost function itself failed to start (missing env
        // var, crashed import, etc.) and Vercel served an HTML error page.
        setErrorMsg(`Checkout failed to start (HTTP ${res.status}, non-JSON response) — check the site's API configuration.`)
        setStatus('error')
        return
      }
      if (data.status !== 'pending' || !data.authorization_url) {
        setErrorMsg(data.message || `Could not start checkout (HTTP ${res.status})`)
        setStatus('error')
        return
      }
      setStatus('redirecting')
      // Boosts are credited server-side once the webhook sees the charge
      // succeed — there's no client-side count to hand back yet, so
      // onBoosted isn't called here. Full-page redirect to Paystack's
      // hosted checkout.
      window.location.href = data.authorization_url
    } catch (e) {
      // A real fetch-level failure (DNS, CORS block, connection refused)
      // — no response was received at all. Showing the exact URL this
      // tried to hit is the fastest way to tell "wrong domain" from
      // "domain's right but something's actually down" without needing
      // browser devtools open.
      setErrorMsg(`Network error — could not reach ${url} (${e instanceof Error ? e.message : 'unknown'}). Try again.`)
      setStatus('error')
    }
  }

  function copyBoostLink() {
    if (!refCode) return
    const url = `${SITE_URL}/servers?ref=${refCode}`
    navigator.clipboard?.writeText(url)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Boost ${guildName}`}
      style={{
        position: 'fixed', inset: 0, zIndex: 50,
        background: 'rgba(5,7,12,0.75)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: '1rem',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: '100%', maxWidth: 440, borderRadius: 12, overflow: 'hidden',
          background: 'var(--pb-surface)', border: '1px solid var(--pb-line)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <BoostHero guildName={guildName} onClose={onClose} />

        <div style={{ padding: '1.25rem' }}>
          <p style={{ fontSize: 12, color: 'var(--pb-text-muted)', marginBottom: 16 }}>
            Boosts push this listing higher in trending, alongside votes and referral conversions.
            Confirming takes you to a secure Paystack checkout — boosts are applied as soon as payment clears.
          </p>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, marginBottom: 16 }}>
            {TIERS.map((t) => (
              <button
                key={t.boosts}
                onClick={() => setSelected(t.boosts)}
                style={{
                  borderRadius: 8, padding: '10px 6px', textAlign: 'center', cursor: 'pointer',
                  background: 'transparent',
                  border: selected === t.boosts ? '2px solid var(--pb-accent)' : '1px solid var(--pb-line)',
                  color: 'var(--pb-text)',
                }}
              >
                {t.popular && (
                  <div style={{
                    fontSize: 10, background: 'rgba(59,130,246,0.15)', color: 'var(--pb-accent)',
                    borderRadius: 6, padding: '1px 6px', display: 'inline-block', marginBottom: 4,
                  }}>
                    Popular
                  </div>
                )}
                <div style={{ fontSize: 13, fontWeight: 500 }}>{t.label}</div>
                <div style={{ fontSize: 12, color: 'var(--pb-text-muted)' }}>${t.price.toFixed(2)}</div>
              </button>
            ))}
            <button
              onClick={() => setSelected(-1)}
              style={{
                borderRadius: 8, padding: '10px 6px', textAlign: 'center', cursor: 'pointer',
                background: 'transparent',
                border: isCustom ? '2px solid var(--pb-accent)' : '1px solid var(--pb-line)',
                color: 'var(--pb-text)',
              }}
            >
              <div style={{ fontSize: 13, fontWeight: 500 }}>Custom</div>
              <div style={{ fontSize: 12, color: 'var(--pb-text-muted)' }}>${PRICE_PER_BOOST.toFixed(2)} each</div>
            </button>
          </div>

          {isCustom && (
            <input
              type="number"
              min={1}
              placeholder="Number of boosts"
              value={customBoosts}
              onChange={(e) => setCustomBoosts(e.target.value)}
              className="pb-input"
              style={{ width: '100%', marginBottom: 16 }}
            />
          )}

          {!isCustom && (
            <div style={{ fontSize: 13, color: 'var(--pb-text-muted)', marginBottom: 16, textAlign: 'center' }}>
              {amount} boosts — ${price.toFixed(2)}
            </div>
          )}
          {isCustom && amount > 0 && (
            <div style={{ fontSize: 13, color: 'var(--pb-text-muted)', marginBottom: 16, textAlign: 'center' }}>
              Total: ${price.toFixed(2)}
            </div>
          )}

          <input
            type="email"
            placeholder="Email for receipt"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="pb-input"
            style={{ width: '100%', marginBottom: 16 }}
          />

          {errorMsg && (
            <p style={{ fontSize: 13, color: 'var(--pb-danger)', marginBottom: 12 }}>{errorMsg}</p>
          )}

          <button
            className="pb-btn-primary w-full"
            disabled={status === 'applying' || status === 'redirecting' || amount <= 0}
            onClick={confirmBoost}
          >
            {status === 'applying' ? 'Starting checkout…' : status === 'redirecting' ? 'Redirecting…' : 'Confirm boost'}
          </button>

          {refCode && status !== 'redirecting' && (
            <button
              onClick={copyBoostLink}
              style={{
                display: 'block', width: '100%', textAlign: 'center', marginTop: 10,
                fontSize: 12, color: 'var(--pb-accent)', background: 'transparent', border: 'none', cursor: 'pointer',
              }}
            >
              {copied ? 'Link copied' : 'Copy boost link'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/** Deep-blue sky with falling streaks of light — same idea as the site's
 * hero, scaled down to modal-header size. Pure CSS keyframes, no canvas,
 * so it costs nothing to mount/unmount with the modal. */
function BoostHero({ guildName, onClose }: { guildName: string; onClose: () => void }) {
  const streaks = Array.from({ length: 16 }).map((_, i) => ({
    left: Math.round(Math.random() * 100),
    delay: (Math.random() * 4).toFixed(2),
    duration: (3 + Math.random() * 3).toFixed(2),
    size: (1.5 + Math.random() * 2).toFixed(1),
  }))
  return (
    <div style={{
      position: 'relative', height: 120, overflow: 'hidden',
      background: 'linear-gradient(180deg,#04122e 0%,#0a2a5c 55%,#123f7a 100%)',
    }}>
      {/* Boost icon (public/boost-icon.png) — black background blends into
          the gradient via mixBlendMode 'screen', so only the glowing blue
          rocket/chevron artwork shows. Replaces the earlier generic
          hero-angels.png crop for this modal specifically. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/boost-icon.png"
        alt=""
        aria-hidden="true"
        style={{
          position: 'absolute', top: '50%', right: 8, transform: 'translateY(-50%)',
          height: '150%', width: 'auto', objectFit: 'contain', mixBlendMode: 'screen',
        }}
      />
      <style>{`
        @keyframes pbBoostFall {
          0% { transform: translateY(-10px); opacity: 0; }
          10% { opacity: 1; }
          100% { transform: translateY(130px); opacity: 0; }
        }
      `}</style>
      {streaks.map((s, i) => (
        <div
          key={i}
          style={{
            position: 'absolute', left: `${s.left}%`, top: '-10px',
            width: `${s.size}px`, height: `${Number(s.size) * 9}px`,
            background: 'linear-gradient(180deg, rgba(255,255,255,0.9), rgba(180,210,255,0))',
            borderRadius: 2,
            animation: `pbBoostFall ${s.duration}s linear ${s.delay}s infinite`,
          }}
        />
      ))}
      <button
        onClick={onClose}
        aria-label="Close"
        style={{
          position: 'absolute', top: 8, right: 8, width: 28, height: 28, borderRadius: 8,
          background: 'rgba(0,0,0,0.35)', color: '#dce8ff', border: 'none', cursor: 'pointer',
        }}
      >
        ×
      </button>
      <div style={{ position: 'absolute', bottom: 12, left: 16, color: '#dce8ff' }}>
        <div style={{ fontSize: 15, fontWeight: 500 }}>Boost {guildName}</div>
        <div style={{ fontSize: 12, color: '#9fb8e6' }}>Climb higher in trending</div>
      </div>
    </div>
  )
}
