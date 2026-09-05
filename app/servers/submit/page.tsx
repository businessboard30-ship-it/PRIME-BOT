// path: app/servers/submit/page.tsx

'use client'

import { useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'

type Prefill = {
  guild_name: string
  guild_icon_url: string | null
  member_count: number
  invite_url: string
  description: string
  tags: string[]
}

type SaveResult = { ref_code?: string | null }

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
const MAX_DESCRIPTION_LEN = 300
const MAX_TAGS = 5

export default function SubmitListingPage() {
  const searchParams = useSearchParams()
  const guildId = searchParams.get('guild_id') || ''
  const token = searchParams.get('token') || ''

  const [prefill, setPrefill] = useState<Prefill | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [inviteUrl, setInviteUrl] = useState('')
  const [description, setDescription] = useState('')
  const [tagInput, setTagInput] = useState('')
  const [tags, setTags] = useState<string[]>([])

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [refCode, setRefCode] = useState<string | null>(null)

  useEffect(() => {
    if (!guildId || !token) {
      setLoadError('This link is missing its guild ID or token — run /servers in Discord again.')
      setLoading(false)
      return
    }
    fetch(`${API_BASE}/api/server_listings?guild_id=${guildId}&token=${encodeURIComponent(token)}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== 'ok') {
          setLoadError(data.message || 'Could not load your server')
        } else {
          setPrefill(data)
          setInviteUrl(data.invite_url || '')
          setDescription(data.description || '')
          setTags(data.tags || [])
        }
      })
      .catch(() => setLoadError('Network error loading your server'))
      .finally(() => setLoading(false))
  }, [guildId, token])

  function addTag() {
    const t = tagInput.trim().replace(/^#/, '').toLowerCase()
    if (!t || tags.includes(t) || tags.length >= MAX_TAGS) return
    setTags([...tags, t])
    setTagInput('')
  }

  async function submit() {
    setSaving(true)
    setSaveError(null)
    setSaved(false)
    try {
      const res = await fetch(
        `${API_BASE}/api/server_listings?guild_id=${guildId}&token=${encodeURIComponent(token)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ invite_url: inviteUrl.trim(), description: description.trim(), tags }),
        }
      )
      const data = await res.json()
      if (data.status !== 'ok') {
        setSaveError(data.message || 'Could not save your listing')
      } else {
        setSaved(true)
        setRefCode((data.listing as SaveResult)?.ref_code || null)
      }
    } catch {
      setSaveError('Network error saving your listing')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return <Shell><p className="text-sm text-neutral-400">Loading…</p></Shell>
  }
  if (loadError || !prefill) {
    return <Shell><p className="text-sm text-red-400">{loadError}</p></Shell>
  }

  return (
    <Shell>
      <h1 className="text-2xl font-semibold tracking-tight mb-1">List your server</h1>
      <p className="text-sm text-neutral-500 mb-8">
        Goes live immediately on <a href="/servers" className="underline">the public directory</a> —
        no approval wait.
      </p>

      <div className="flex items-center gap-3 mb-8 p-3 rounded-lg border border-neutral-800 bg-neutral-900/40">
        {prefill.guild_icon_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={prefill.guild_icon_url} alt="" className="w-12 h-12 rounded-full" />
        ) : (
          <div className="w-12 h-12 rounded-full bg-neutral-800 flex items-center justify-center text-neutral-500">
            {prefill.guild_name.slice(0, 1).toUpperCase()}
          </div>
        )}
        <div>
          <p className="font-medium">{prefill.guild_name}</p>
          <p className="text-xs text-neutral-500">{prefill.member_count.toLocaleString()} members</p>
        </div>
        <span className="ml-auto text-xs text-neutral-600">from Discord — not editable here</span>
      </div>

      <label className="block mb-6">
        <span className="text-sm font-medium">Invite link</span>
        <input
          className="input mt-1.5 w-full"
          placeholder="https://discord.gg/yourinvite"
          value={inviteUrl}
          onChange={(e) => setInviteUrl(e.target.value)}
        />
        <span className="text-xs text-neutral-500 mt-1 block">
          Use a permanent, never-expiring invite if you have one.
        </span>
      </label>

      <label className="block mb-6">
        <span className="text-sm font-medium">Description</span>
        <textarea
          className="input mt-1.5 w-full resize-none"
          rows={3}
          maxLength={MAX_DESCRIPTION_LEN}
          placeholder="What's your server about?"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <span className="text-xs text-neutral-500 mt-1 block">
          {description.length}/{MAX_DESCRIPTION_LEN}
        </span>
      </label>

      <label className="block mb-8">
        <span className="text-sm font-medium">Tags (up to {MAX_TAGS})</span>
        <div className="flex flex-wrap gap-2 mt-1.5 mb-2">
          {tags.map((t) => (
            <span key={t} className="chip">
              #{t}
              <button className="chip-remove" onClick={() => setTags(tags.filter((x) => x !== t))} aria-label={`Remove ${t}`}>
                ×
              </button>
            </span>
          ))}
        </div>
        {tags.length < MAX_TAGS && (
          <form onSubmit={(e) => { e.preventDefault(); addTag() }} className="flex gap-2">
            <input
              className="input flex-1"
              placeholder="e.g. gaming, anime, coding"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
            />
            <button type="submit" className="btn-secondary">Add</button>
          </form>
        )}
      </label>

      <button className="btn-primary" onClick={submit} disabled={saving || !inviteUrl.trim()}>
        {saving ? 'Saving…' : 'Publish listing'}
      </button>
      {saveError && <p className="text-sm text-red-400 mt-3">{saveError}</p>}
      {saved && (
        <div className="mt-3">
          <p className="text-sm text-emerald-500">
            ✅ Saved — <a href="/servers" className="underline">view it on the directory</a>.
          </p>
          {refCode && (
            <p className="text-xs text-neutral-500 mt-2">
              Your boost link (share it — joins through it raise your ranking):{' '}
              <code className="chip !px-2 !py-0.5">{`/servers?ref=${refCode}`}</code>
            </p>
          )}
        </div>
      )}

      <p className="text-xs text-neutral-500 mt-10">
        This link edits your listing — don't share it outside your admin team. Rerun{' '}
        <code className="chip !px-2 !py-0.5">/servers</code> in Discord any time to get it again.
      </p>
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100 px-6 py-12">
      <div className="max-w-xl mx-auto">{children}</div>
      <style jsx global>{`
        .input {
          background: #171717;
          border: 1px solid #333;
          border-radius: 6px;
          padding: 8px 10px;
          font-size: 14px;
          color: inherit;
        }
        .btn-primary {
          background: #2563eb;
          border-radius: 8px;
          padding: 8px 18px;
          font-size: 14px;
          font-weight: 500;
          color: white;
          border: none;
          cursor: pointer;
        }
        .btn-primary:hover {
          background: #1d4ed8;
        }
        .btn-primary:disabled {
          background: #404040;
          cursor: not-allowed;
        }
        .btn-secondary {
          background: #262626;
          border: 1px solid #404040;
          border-radius: 6px;
          padding: 6px 14px;
          font-size: 14px;
          cursor: pointer;
        }
        .btn-secondary:hover {
          background: #333;
        }
        .chip {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          background: #262626;
          border: 1px solid #404040;
          border-radius: 999px;
          padding: 2px 4px 2px 10px;
          font-size: 13px;
        }
        .chip-remove {
          background: transparent;
          border: none;
          color: #a3a3a3;
          cursor: pointer;
          font-size: 15px;
          line-height: 1;
          padding: 2px 6px;
        }
        .chip-remove:hover {
          color: #fff;
        }
      `}</style>
    </main>
  )
}
