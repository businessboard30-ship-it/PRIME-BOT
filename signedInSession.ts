// path: app/_signedInSession.ts

'use client'

/**
 * Site-wide "am I signed in" state, shared by every page that shows a
 * Sign in / avatar+Sign out control (currently _ServerDirectory.tsx and
 * app/login/servers/page.tsx).
 *
 * There's still no cookie/account system in this repo (see
 * discord_login_oauth.py's docstring) — a "session" is just an opaque id
 * that resolves to a 30-minute-TTL row in discord_login_sessions. Before
 * this file, that id only ever lived in the /login/servers URL, so it was
 * invisible everywhere else the second you clicked to a different page —
 * which is why "Sign in" kept showing even after a successful sign-in.
 * Stashing the id in localStorage (not a cookie — nothing server-side
 * needs to read it automatically) lets any page ask "is there a session
 * id, and does it still resolve?" and render accordingly. Same 30-minute
 * lifetime either way: once the row expires server-side, the resolve
 * fetch below 404s and this clears itself out.
 */

import { useEffect, useState } from 'react'

const STORAGE_KEY = 'pb_login_session'

export type SignedInUser = {
  id: string
  username: string
  avatar_url: string
}

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

export function rememberLoginSession(sessionId: string) {
  try {
    localStorage.setItem(STORAGE_KEY, sessionId)
  } catch {
    // Private browsing / storage disabled — sign-in still works for this
    // page load via the URL param, it just won't carry over to other
    // pages. Not worth surfacing an error for.
  }
}

export function forgetLoginSession() {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // see rememberLoginSession
  }
}

/** Reads whatever session id is stashed locally and resolves it against
 * the server. Returns null (not "loading") once resolved with nothing
 * signed-in, so callers can render the "Sign in" button immediately. */
export function useSignedInUser(): { user: SignedInUser | null; loading: boolean; signOut: () => void } {
  const [user, setUser] = useState<SignedInUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let sessionId: string | null = null
    try {
      sessionId = localStorage.getItem(STORAGE_KEY)
    } catch {
      sessionId = null
    }
    if (!sessionId) {
      setLoading(false)
      return
    }
    fetch(`${API_BASE}/api/discord_login_oauth?session=${encodeURIComponent(sessionId)}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status === 'ok' && data.user) {
          setUser(data.user)
        } else {
          forgetLoginSession()
        }
      })
      .catch(() => {
        // Network hiccup, not "session invalid" — leave the stored id
        // alone so the next page load can retry instead of forcing a
        // fresh sign-in over a transient failure.
      })
      .finally(() => setLoading(false))
  }, [])

  function signOut() {
    let sessionId: string | null = null
    try {
      sessionId = localStorage.getItem(STORAGE_KEY)
    } catch {
      sessionId = null
    }
    forgetLoginSession()
    setUser(null)
    if (sessionId) {
      fetch(`${API_BASE}/api/discord_login_oauth?session=${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
      }).catch(() => {
        // Best-effort — see the DELETE-route docstring in
        // discord_login_oauth.py: worst case is a low-value session row
        // sitting around for up to 30 more minutes, not a security gap.
      })
    }
  }

  return { user, loading, signOut }
}
