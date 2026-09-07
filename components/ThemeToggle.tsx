// path: components/ThemeToggle.tsx

'use client'

import { useEffect, useState } from 'react'

// Site already follows prefers-color-scheme automatically (see
// app/layout.tsx's viewport.colorScheme + globals.css's :root/.dark
// variables) — this adds a manual override on top: a click sets a 'dark'
// or 'light' class on <html> that beats the system preference, persisted
// so it sticks across visits. Absence of the override (first visit) falls
// back to whatever the system already renders.
const STORAGE_KEY = 'pb-theme-override'

export default function ThemeToggle() {
  const [mode, setMode] = useState<'light' | 'dark' | null>(null)

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') {
      setMode(saved)
      document.documentElement.classList.toggle('dark', saved === 'dark')
    }
  }, [])

  function toggle() {
    const current = mode ?? (document.documentElement.classList.contains('dark') ? 'dark' : 'light')
    const next = current === 'dark' ? 'light' : 'dark'
    setMode(next)
    document.documentElement.classList.toggle('dark', next === 'dark')
    window.localStorage.setItem(STORAGE_KEY, next)
  }

  return (
    <button
      onClick={toggle}
      aria-label="Toggle dark and light theme"
      className="pb-btn-secondary text-sm px-3 py-1.5"
      title="Toggle theme"
    >
      {mode === 'dark' ? '☀️ Light' : mode === 'light' ? '🌙 Dark' : '🌓 Theme'}
    </button>
  )
}
