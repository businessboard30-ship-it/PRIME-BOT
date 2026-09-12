// path: app/unlock/unlock-status.tsx

'use client'

import { useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'

// Served by api_server.py (Railway), same convention app/dashboard/[guildId]/page.tsx
// already uses — NOT a Next.js API route. This frontend has no direct DB
// access; api/selar_redirect.py, api/selar_submit.py and api/selar_status.py
// are the only things allowed to touch payment_logs for this flow.
const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

type Status = 'pending' | 'awaiting_review' | 'verified' | 'rejected' | 'invalid'

export function UnlockStatus() {
  const params = useSearchParams()

  // Everything here was placed by api/selar_redirect.py after it verified
  // the HMAC signature payments_manual.py baked into the original Selar
  // pay link — this page never re-derives or trusts anything a visitor
  // could have typed in by hand. A visitor who reaches /unlock any other
  // way (guessed reference, bookmarked an old link, edited the URL) has
  // state=invalid or no state at all, and gets told so below rather than
  // ever seeing an "I've Paid" button.
  const reference = params.get('reference') ?? ''
  const paymentType = params.get('payment_type') ?? ''
  const buyerId = params.get('buyer_id') ?? ''
  const sig = params.get('sig') ?? ''
  const ts = params.get('ts') ?? ''
  const guildId = params.get('guild_id') ?? ''
  const cloneId = params.get('clone_id') ?? ''
  const cameFromValidRedirect = params.get('state') === 'pending' && !!reference && !!sig

  const [status, setStatus] = useState<Status>(cameFromValidRedirect ? 'pending' : 'invalid')
  const [submitted, setSubmitted] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!cameFromValidRedirect) return
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
        if (body.submitted) setSubmitted(true)
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
  }, [reference, cameFromValidRedirect])

  async function submitPaid() {
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
        body: JSON.stringify({
          reference,
          payment_type: paymentType,
          buyer_id: buyerId,
          sig,
          ts,
          guild_id: guildId || undefined,
          clone_id: cloneId || undefined,
        }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(body.message ?? 'Unable to submit payment')
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

  const finalized = status === 'verified' || status === 'rejected'
  const label =
    status === 'invalid'
      ? 'Link invalid'
      : status === 'verified'
        ? 'Unlocked'
        : status === 'rejected'
          ? 'Not verified'
          : submitted
            ? 'Submitted — awaiting confirmation'
            : 'Awaiting confirmation'

  return (
    <main className="pb-page px-6 py-16">
      <div className="mx-auto max-w-xl space-y-8">
        <div className="space-y-3">
          <p className="pb-heading text-xs" style={{ color: 'var(--pb-text-muted)' }}>
            PRIME-BOT / Unlock
          </p>
          <h1 className="text-3xl font-semibold tracking-tight" style={{ color: 'var(--pb-text)' }}>
            {status === 'verified' ? 'Your unlock is active.' : status === 'invalid' ? 'This link isn\u2019t valid' : 'Confirm your payment'}
          </h1>
        </div>

        <section
          className="rounded-2xl p-6"
          style={{ background: 'var(--pb-surface)', border: '1px solid var(--pb-line)' }}
        >
          {status === 'invalid' ? (
            <p className="text-sm leading-6" style={{ color: 'var(--pb-text-muted)' }}>
              This confirmation link is missing or invalid. Reopen the payment from Discord and try again — this
              page is only reachable right after completing checkout on Selar.
            </p>
          ) : (
            <>
              <p className="text-sm font-medium" style={{ color: 'var(--pb-text)' }}>Order details</p>
              <p className="mt-2 text-xl font-semibold" style={{ color: 'var(--pb-text)' }}>
                {paymentType.replace(/_/g, ' ')}
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
                    : "Tap \u201cI've Paid\u201d after completing checkout. This notifies the administrator but does not unlock access on its own."}
              </p>
              {error && <p className="mt-3 text-sm" style={{ color: 'var(--pb-danger)' }}>{error}</p>}
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
