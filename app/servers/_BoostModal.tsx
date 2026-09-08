// path: app/servers/_BoostModal.tsx

'use client'

/**
 * Boost purchase modal, opened from the "Boost" button on a listing card
 * in _ServerDirectory.tsx (replacing the old inline "Copy boost link"
 * button — that link now lives here instead, alongside the boost tiers).
 *
 * PAYMENT IS NOT WIRED YET. Picking a tier and hitting "Confirm boost"
 * calls api/apply_boost.py directly, which currently trusts the client
 * and applies the boost immediately — see that file's header for what's
 * needed before this can take real money. Everything else here (the
 * tiers, the apply call, the trending-sort effect on the backend, the
 * boost-link copy) is real and working.
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
  const [status, setStatus] = useState<'idle' | 'applying' | 'done' | 'error'>('idle')
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const isCustom = selected === -1
  const customAmount = Math.max(0, parseInt(customBoosts || '0', 10) || 0)
  const amount = isCustom ? customAmount : selected
  const price = isCustom ? customAmount * PRICE_PER_BOOST : (TIERS.find((t) => t.boosts === selected)?.price ?? 0)

  async function confirmBoost() {
    if (amount <= 0) {
      setErrorMsg('Choose a boost amount first')
      return
    }
    setErrorMsg(null)
    setStatus('applying')
    try {
      const res = await fetch(`${API_BASE}/api/apply_boost`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ guild_id: guildId, clone_id: cloneId, amount }),
      })
      const data = await res.json()
      if (data.status !== 'ok') {
        setErrorMsg(data.message || 'Could not apply boost')
        setStatus('error')
        return
      }
      setStatus('done')
      onBoosted(data.boost_count)
    } catch {
      setErrorMsg('Network error — try again')
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
            Payment isn&apos;t wired up in this preview — confirming applies the boost directly.
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

          {errorMsg && (
            <p style={{ fontSize: 13, color: 'var(--pb-danger)', marginBottom: 12 }}>{errorMsg}</p>
          )}

          {status === 'done' ? (
            <div style={{ textAlign: 'center' }}>
              <p style={{ fontSize: 13, color: 'var(--pb-positive)', marginBottom: 12 }}>
                Boost applied — {amount} boosts added.
              </p>
              <button className="pb-btn-secondary w-full" onClick={onClose}>Done</button>
            </div>
          ) : (
            <button
              className="pb-btn-primary w-full"
              disabled={status === 'applying' || amount <= 0}
              onClick={confirmBoost}
            >
              {status === 'applying' ? 'Applying…' : 'Confirm boost'}
            </button>
          )}

          {refCode && status !== 'done' && (
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
