// path: app/servers/_AddServerButton.tsx

'use client'

/**
 * "+ Add your server" button shared by the directory header
 * (_ServerDirectory.tsx) and the per-listing deep-link page
 * ([guildId]/page.tsx) — pulled out into its own tiny client component so
 * the deep-link page (a server component, for its generateMetadata OG
 * tags) can still use it without itself becoming 'use client'.
 *
 * PRIME-BOT is now OPTIONAL for listing a server (see
 * api/discord_login_oauth.py's bot_present flag and /login/servers) — a
 * listing just needs Discord OAuth to prove Manage Server on the guild,
 * not the bot's presence. So tapping this button offers a real choice
 * instead of assuming either path:
 *   - "Add PRIME-BOT to a server" — the traditional invite link, for
 *     someone who wants the bot's other features (moderation, leveling,
 *     etc.) alongside the listing.
 *   - "Skip — sign in with Discord" — goes straight into
 *     api/discord_login_oauth.py's flow, which now lists EVERY guild the
 *     signed-in user manages (bot or no bot) with a "List this server"
 *     link straight to the real submit form.
 */

import { useEffect, useRef, useState } from 'react'

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const DISCORD_CLIENT_ID = process.env.NEXT_PUBLIC_DISCORD_CLIENT_ID || ''
const BOT_INVITE_URL = DISCORD_CLIENT_ID
  ? `https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&scope=bot+applications.commands&permissions=8`
  : ''

export default function AddServerButton({ className }: { className?: string }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    function onClickOutside(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    function onEscape(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', onClickOutside)
    window.addEventListener('keydown', onEscape)
    return () => {
      window.removeEventListener('mousedown', onClickOutside)
      window.removeEventListener('keydown', onEscape)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative inline-block">
      <button type="button" className={className} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        + Add your server
      </button>
      {open && (
        <div
          className="absolute right-0 mt-1 z-20 rounded-md border overflow-hidden w-64 text-sm"
          style={{ background: 'var(--pb-surface)', borderColor: 'var(--pb-line)' }}
        >
          {BOT_INVITE_URL && (
            <a
              href={BOT_INVITE_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="block px-3 py-2.5 hover:bg-white/5"
              onClick={() => setOpen(false)}
            >
              <span className="block font-medium">Add PRIME-BOT to a server</span>
              <span className="block text-xs" style={{ color: 'var(--pb-text-faint)' }}>
                Gets you moderation, leveling, and more too
              </span>
            </a>
          )}
          <a
            href={`${API_BASE}/api/discord_login_oauth`}
            className="block px-3 py-2.5 hover:bg-white/5"
            style={{ borderTop: BOT_INVITE_URL ? '1px solid var(--pb-line)' : undefined }}
            onClick={() => setOpen(false)}
          >
            <span className="block font-medium">Skip — sign in with Discord</span>
            <span className="block text-xs" style={{ color: 'var(--pb-text-faint)' }}>
              List a server without adding the bot
            </span>
          </a>
        </div>
      )}
    </div>
  )
}
