// path: app/unlock/unlock-status.tsx

'use client'

import { useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'

// Served by api_server.py (Railway), same convention app/dashboard/[guildId]/page.tsx
// already uses — NOT a Next.js API route. This frontend has no direct DB
// access; api/discord_login_oauth.py, api/selar_submit.py and
// api/selar_status.py are the only things allowed to touch payment_logs /
// login sessions for this flow.
const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

type Status = 'pending' | 'awaiting_review' | 'verified' | 'rejected' | 'invalid'
type Identity = { id: string; username: string; avatar_url: string }

// Selar's product-level "redirect after purchase" is a single STATIC url
// per product with nothing appended (confirmed) — every buyer of a given
// product lands here with only ?payment_type=<type> in the URL. There is
// no reference/sig/buyer_id to trust anymore (that was the old signed-
// redirect design — see payments_manual.py's docstring); identity here
// comes entirely from signing in with Discord below.
export function UnlockStatus() {
  const params = useSearchParams()
  const paymentType = params.get('payment_type') ?? ''
  const sessionParam = params.get('session') ?? ''

  const [sessionId, setSessionId] = useState('')
  const [identity, setIdentity] = useState<Identity | null>(null)
  const [identityError, setIdentityError] = useState('')
  const [loadingIdentity, setLoadingIdentity] = useState(!!sessionParam)

  const [reference, setReference] = useState('')
  const [status, setStatus] = useState<Status>('pending')
  const [submitted, setSubmitted] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Leg 3 of api/discord_login_oauth.py: exchange the session id the
  // callback redirected us with for the identity Discord's own OAuth
  // response produced (never anything read from our own query string).
  useEffect(() => {
    if (!sessionParam) return
    let cancelled = false
    const loadIdentity = async () => {
      try {
        const response = await fetch(
          `${API_BASE}/api/discord_login_oauth?session=${encodeURIComponent(sessionParam)}`,
          { cache: 'no-store' }
        )
        const body = await response.json().catch(() => ({}))
        if (cancelled) return
        if (!response.ok || body.status !== 'ok' || !body.user?.id) {
          setIdentityError('That sign-in link expired. Please sign in again.')
          return
        }
        setSessionId(sessionParam)
        setIdentity(body.user)
      } catch {
        if (!cancelled) setIdentityError('Sign-in is temporarily unavailable. Please try again shortly.')
      } finally {
        if (!cancelled) setLoadingIdentity(false)
      }
    }
    void loadIdentity()
    return () => {
      cancelled = true
    }
  }, [sessionParam])

  // Poll selar_status once we have a reference (only exists after a
  // successful "I've Paid" claim, since there's nothing to poll before that).
  useEffect(() => {
    if (!reference) return
    let cancelled = false
    const check = async () => {
      try {
        const response = await fetch(
          `${API_BASE}/api/selar_status?reference=${encodeURIComponent(reference)}`,
          { cache: 'no-store' }
        )
        const body = await response.json()
        if (cancelled) return
        if (body.status) setStatus(body.status)
      } catch {
        if (!cancelled) setError('Status is temporarily unavailable. Please try again shortly.')
      }
    }
    void check()
    const timer = window.setInterval(check, 5000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [reference])

  function signInUrl() {
    const returnTo = `/unlock?payment_type=${encodeURIComponent(paymentType)}`
    return `${API_BASE}/api/discord_login_oauth?return_to=${encodeURIComponent(returnTo)}`
  }

  async function submitPaid() {
    if (!sessionId || !paymentType) return
    // Dim/disable the instant the tap happens, before the request even
    // resolves — the server-side atomic claim (db.claim_manual_payment_for_review)
    // is what actually prevents a duplicate admin DM, but this stops an
    // impatient double-tap from firing two requests in the first place.
    setSubmitted(true)
    setSubmitting(true)
    setError('')
    try {
      const response = await fetch(`${API_BASE}/api/selar_submit`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, payment_type: paymentType }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(body.message ?? 'Unable to submit payment')
      if (body.reference) setReference(body.reference)
      setStatus(body.status ?? 'awaiting_review')
    } catch (reason) {
      // Roll the dim back only on a real failure, so a genuine network
      // error doesn't strand the buyer with a permanently disabled button.
      setSubmitted(false)
      setError(reason instanceof Error ? reason.message : 'Unable to submit payment')
    } finally {
      setSubmitting(false)
    }
  }

  const missingPaymentType = !paymentType
  const finalized = status === 'verified' || status === 'rejected'
  const label =
    status === 'verified'
      ? 'Unlocked'
      : status === 'rejected'
        ? 'Not verified'
        : submitted
          ? 'Reported — awaiting confirmation'
          : 'Awaiting confirmation'

  return (
    <main className="pb-page px-6 py-16">
      <div className="mx-auto max-w-xl space-y-8">
        <div className="space-y-3">
          <p className="pb-heading text-xs" style={{ color: 'var(--pb-text-muted)' }}>
            PRIME-BOT / Unlock
          </p>
          <h1 className="text-3xl font-semibold tracking-tight" style={{ color: 'var(--pb-text)' }}>
            {status === 'verified' ? 'Your unlock is active.' : missingPaymentType ? 'This link isn\u2019t valid' : 'Confirm your payment'}
          </h1>
        </div>

        <section
          className="rounded-2xl p-6"
          style={{ background: 'var(--pb-surface)', border: '1px solid var(--pb-line)' }}
        >
          {missingPaymentType ? (
            <p className="text-sm leading-6" style={{ color: 'var(--pb-text-muted)' }}>
              This confirmation link is missing what it was for. Reopen the payment from Discord and try again.
            </p>
          ) : (
            <>
              <p className="text-sm font-medium" style={{ color: 'var(--pb-text)' }}>Order details</p>
              <p className="mt-2 text-xl font-semibold" style={{ color: 'var(--pb-text)' }}>
                {paymentType.replace(/_/g, ' ')}
              </p>

              {!identity ? (
                <>
                  <p className="mt-4 text-sm leading-6" style={{ color: 'var(--pb-text-muted)' }}>
                    Sign in with the same Discord account you used in the bot, so we know whose payment to check.
                  </p>
                  {identityError && <p className="mt-3 text-sm" style={{ color: 'var(--pb-danger)' }}>{identityError}</p>}
                  <a href={signInUrl()} className="pb-btn-primary mt-6 inline-flex">
                    {loadingIdentity ? 'Signing in…' : 'Sign in with Discord'}
                  </a>
                </>
              ) : (
                <>
                  <p className="mt-4 text-sm" style={{ color: 'var(--pb-text-muted)' }}>
                    Signed in as <span style={{ color: 'var(--pb-text)' }}>{identity.username}</span>
                  </p>
                  {reference && (
                    <p className="mt-4 break-all font-mono text-xs" style={{ color: 'var(--pb-text-faint)' }}>
                      Reference: {reference}
                    </p>
                  )}

                  {!finalized && !submitted && (
                    <button
                      type="button"
                      onClick={submitPaid}
                      disabled={submitting}
                      className="pb-btn-primary mt-6"
                    >
                      {submitting ? 'Submitting…' : "I've Paid"}
                    </button>
                  )}
                  {!finalized && submitted && (
                    <button type="button" disabled className="pb-btn-primary mt-6 opacity-50 cursor-not-allowed">
                      {submitting ? 'Submitting…' : 'Reported — awaiting confirmation'}
                    </button>
                  )}

                  <p className="mt-6 text-sm leading-6" style={{ color: 'var(--pb-text-muted)' }}>
                    {finalized
                      ? label
                      : submitted
                        ? 'Reported — awaiting confirmation. The administrator was notified and will review your payment against the Selar dashboard shortly.'
                        : "Tap \u201cI've Paid\u201d after completing checkout on Selar. This notifies the administrator but does not unlock access on its own."}
                  </p>
                  {error && <p className="mt-3 text-sm" style={{ color: 'var(--pb-danger)' }}>{error}</p>}
                </>
              )}
            </>
          )}
        </section>

        <a className="pb-btn-secondary inline-flex" href="/">
          Return to PRIME-BOT
        </a>
      </div>
    </main>
  )
}
