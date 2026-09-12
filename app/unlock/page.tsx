// path: app/unlock/page.tsx

import { Suspense } from 'react'
import { UnlockStatus } from './unlock-status'

export default function UnlockPage() {
  return (
    <Suspense
      fallback={
        <main className="pb-page px-6 py-16">
          <div className="mx-auto max-w-xl">
            <p className="text-sm" style={{ color: 'var(--pb-text-muted)' }}>Checking unlock status…</p>
          </div>
        </main>
      }
    >
      <UnlockStatus />
    </Suspense>
  )
}
