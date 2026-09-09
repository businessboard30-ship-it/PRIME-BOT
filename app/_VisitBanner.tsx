// path: app/_VisitBanner.tsx

'use client'

/**
 * Small strip at the very top of every page showing the running site-visit
 * count (api/site_visits.py). Sits in normal document flow (not fixed/
 * absolute) so it can never end up compositing over other content the way
 * the /login/servers header briefly did during a route transition —
 * see that page's Shell component for the isolate/opaque-background fix
 * that bug needed.
 */

import { useEffect, useState } from 'react'

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

export default function VisitBanner() {
  const [count, setCount] = useState<number | null>(null)

  useEffect(() => {
    fetch(`${API_BASE}/api/site_visits`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status === 'ok' && typeof data.count === 'number') {
          setCount(data.count)
        }
      })
      .catch(() => {
        // Non-essential — just don't show the banner rather than surfacing
        // an error for something this cosmetic.
      })
  }, [])

  if (count === null) return null

  return (
    <div
      className="w-full text-center text-xs py-1.5"
      style={{
        background: 'var(--pb-surface)',
        borderBottom: '1px solid var(--pb-line)',
        color: 'var(--pb-text-faint)',
      }}
    >
      👁 {count.toLocaleString()} site visits
    </div>
  )
}
