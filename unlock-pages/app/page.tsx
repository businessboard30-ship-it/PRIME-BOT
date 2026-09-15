// path: app/page.tsx

// Static export has no server-side redirect, so this is a client-side
// bounce straight to /unlock (the only page this standalone site hosts).
'use client'

import { useEffect } from 'react'

export default function RootPage() {
  useEffect(() => {
    window.location.replace('./unlock/' + window.location.search)
  }, [])
  return null
}
