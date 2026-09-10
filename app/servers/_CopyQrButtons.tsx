// path: app/servers/_CopyQrButtons.tsx

'use client'

/**
 * Copy-invite-link + QR-code popover for a listing's invite_url. Used to
 * live on every directory card (_ServerDirectory.tsx) — moved here, to the
 * per-listing deep-link page only, since two extra buttons per card added
 * clutter to a list of dozens of listings that most people never tapped;
 * anyone who wants them has already tapped through to this one server's
 * page. [guildId]/page.tsx is a server component (for its generateMetadata
 * OG tags), so this interactive bit lives in its own small client
 * component, same pattern as _AddServerButton.tsx.
 */

import { useState } from 'react'

export default function CopyQrButtons({ guildName, inviteUrl }: { guildName: string; inviteUrl: string }) {
  const [copied, setCopied] = useState(false)
  const [showQr, setShowQr] = useState(false)

  async function copyInvite() {
    try {
      await navigator.clipboard.writeText(inviteUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      window.prompt('Copy this invite link:', inviteUrl)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-1">
        <button
          type="button"
          className="text-sm px-3 py-1.5 rounded-md"
          style={{ background: 'var(--pb-surface-raised)', border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
          title="Copy invite link"
          onClick={copyInvite}
        >
          {copied ? 'Copied ✓' : 'Copy invite link'}
        </button>
        <button
          type="button"
          className="text-sm px-3 py-1.5 rounded-md"
          style={{ background: 'var(--pb-surface-raised)', border: '1px solid var(--pb-line)', color: 'var(--pb-text-faint)' }}
          title="Show QR code"
          onClick={() => setShowQr((s) => !s)}
        >
          QR code
        </button>
      </div>
      {showQr && (
        // Same public QR-image API the card used to call — no new
        // dependency or backend endpoint needed for this.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={`https://api.qrserver.com/v1/create-qr-code/?size=140x140&data=${encodeURIComponent(inviteUrl)}`}
          alt={`QR code for ${guildName} invite`}
          className="w-32 h-32 rounded-md"
          style={{ background: '#fff', padding: '4px' }}
        />
      )}
    </div>
  )
}
