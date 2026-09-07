// path: app/servers/submit/page.tsx

'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import Image from 'next/image'

type Prefill = {
  guild_name: string
  guild_icon_url: string | null
  member_count: number
  invite_url: string
  description: string
  tags: string[]
  banner_url?: string
}

type SaveResult = { ref_code?: string | null }

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''
// Same categories shown as filter chips on the public directory
// (app/servers/_ServerDirectory.tsx) — kept in sync manually since there's
// no shared constants file yet. Suggestions shown while typing a tag below.
const TAG_SUGGESTIONS = ['gaming', 'anime', 'coding', 'art', 'music', 'study', 'crypto', 'nsfw']
const MAX_DESCRIPTION_LEN = 300
const MAX_TAGS = 5

// Next.js requires useSearchParams() to be wrapped in a Suspense boundary
// for static prerendering — without it, the build fails at "Generating
// static pages" with "useSearchParams() should be wrapped in a suspense
// boundary" (this page has no static content worth prerendering anyway,
// it's 100% query-param-driven, so the fallback just flashes briefly).
export default function SubmitListingPage() {
  return (
    <Suspense fallback={<Shell><p className="text-sm" style={{ color: 'var(--pb-text-muted)' }}>Loading…</p></Shell>}>
      <SubmitListingPageInner />
    </Suspense>
  )
}

function SubmitListingPageInner() {
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
  const [bannerUrl, setBannerUrl] = useState('')

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
    // /setup servers (discord_bot/cogs/server_listing.py) may attach its
    // own best-effort invite_url/description straight onto this page's URL
    // for a brand-new listing (an auto-generated permanent invite, and the
    // guild's Community description if it has one). Those are just a
    // starting point for the form, though — anything already saved in the
    // DB (returned by the API below) is the real prefill and always wins,
    // so re-visiting an existing listing's link never clobbers what the
    // admin already wrote here.
    const urlInviteUrl = searchParams.get('invite_url') || ''
    const urlDescription = searchParams.get('description') || ''
    fetch(`${API_BASE}/api/server_listings?guild_id=${guildId}&token=${encodeURIComponent(token)}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== 'ok') {
          setLoadError(data.message || 'Could not load your server')
        } else {
          setPrefill(data)
          setInviteUrl(data.invite_url || urlInviteUrl)
          setDescription(data.description || urlDescription)
          setTags(data.tags || [])
          setBannerUrl(data.banner_url || '')
        }
      })
      .catch(() => setLoadError('Network error loading your server'))
      .finally(() => setLoading(false))
  }, [guildId, token, searchParams])

  function addTag() {
    const t = tagInput.trim().replace(/^#/, '').toLowerCase()
    if (!t || tags.includes(t) || tags.length >= MAX_TAGS) return
    setTags([...tags, t])
    setTagInput('')
  }

  // Solves the self-hosted proof-of-work captcha in-browser (see
  // pow_captcha.py) — brute-forces `nonce` until sha256(`${salt}:${nonce}`)
  // starts with `difficulty` hex zeros. At the default difficulty (4) this
  // takes a fraction of a second on any real device; no external captcha
  // vendor, no widget, nothing to configure beyond the backend's own
  // POW_SECRET_KEY env var.
  async function solvePow(challenge: { salt: string; difficulty: number; expires: number; signature: string }) {
    const encoder = new TextEncoder()
    const target = '0'.repeat(challenge.difficulty)
    for (let nonce = 0; ; nonce++) {
      const data = encoder.encode(`${challenge.salt}:${nonce}`)
      const hashBuffer = await crypto.subtle.digest('SHA-256', data)
      const hex = Array.from(new Uint8Array(hashBuffer)).map((b) => b.toString(16).padStart(2, '0')).join('')
      if (hex.startsWith(target)) {
        return { ...challenge, nonce: String(nonce) }
      }
    }
  }

  async function submit() {
    setSaving(true)
    setSaveError(null)
    setSaved(false)
    try {
      const challengeRes = await fetch(`${API_BASE}/api/server_listings?pow_challenge=1`)
      const challengeData = await challengeRes.json()
      if (challengeData.status !== 'ok') {
        setSaveError('Could not start the anti-spam check — try again')
        setSaving(false)
        return
      }
      const pow = await solvePow(challengeData)

      const res = await fetch(
        `${API_BASE}/api/server_listings?guild_id=${guildId}&token=${encodeURIComponent(token)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            invite_url: inviteUrl.trim(), description: description.trim(), tags,
            banner_url: bannerUrl.trim(), pow,
          }),
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
    return <Shell><p className="text-sm" style={{ color: 'var(--pb-text-muted)' }}>Loading…</p></Shell>
  }
  if (loadError || !prefill) {
    return <Shell><p className="text-sm" style={{ color: 'var(--pb-danger)' }}>{loadError}</p></Shell>
  }

  return (
    <Shell>
      <h1 className="pb-heading text-2xl font-semibold mb-1">List your server</h1>
      <p className="text-sm mb-8" style={{ color: 'var(--pb-text-faint)' }}>
        Goes live immediately on <a href="/servers" className="underline">the public directory</a> —
        no approval wait.
      </p>

      <div
        className="flex items-center gap-3 mb-8 p-3 rounded-lg border"
        style={{ borderColor: 'var(--pb-line)', background: 'var(--pb-surface)' }}
      >
        {prefill.guild_icon_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={prefill.guild_icon_url} alt="" className="w-12 h-12 rounded-full" />
        ) : (
          <div
            className="w-12 h-12 rounded-full flex items-center justify-center"
            style={{ background: 'var(--pb-surface-raised)', color: 'var(--pb-text-faint)' }}
          >
            {prefill.guild_name.slice(0, 1).toUpperCase()}
          </div>
        )}
        <div>
          <p className="font-medium">{prefill.guild_name}</p>
          <p className="text-xs" style={{ color: 'var(--pb-text-faint)' }}>
            {prefill.member_count.toLocaleString()} members
          </p>
        </div>
        <span className="ml-auto text-xs" style={{ color: 'var(--pb-text-faint)' }}>
          from Discord — not editable here
        </span>
      </div>

      <label className="block mb-6">
        <span className="text-sm font-medium">Invite link</span>
        <input
          className="pb-input mt-1.5 w-full"
          placeholder="https://discord.gg/yourinvite"
          value={inviteUrl}
          onChange={(e) => setInviteUrl(e.target.value)}
        />
        <span className="text-xs mt-1 block" style={{ color: 'var(--pb-text-faint)' }}>
          Use a permanent, never-expiring invite if you have one.
        </span>
      </label>

      <label className="block mb-6">
        <span className="text-sm font-medium">Description</span>
        <textarea
          className="pb-input mt-1.5 w-full resize-none"
          rows={3}
          maxLength={MAX_DESCRIPTION_LEN}
          placeholder="What's your server about?"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <span className="text-xs mt-1 block" style={{ color: 'var(--pb-text-faint)' }}>
          {description.length}/{MAX_DESCRIPTION_LEN}
        </span>
      </label>

      <label className="block mb-6">
        <span className="text-sm font-medium">Banner image URL (optional)</span>
        <input
          className="pb-input mt-1.5 w-full"
          placeholder="https://example.com/banner.png"
          value={bannerUrl}
          onChange={(e) => setBannerUrl(e.target.value)}
        />
        <span className="text-xs mt-1 block" style={{ color: 'var(--pb-text-faint)' }}>
          Wide image shown at the top of your listing's page and in link previews. Host it anywhere public.
        </span>
      </label>

      <label className="block mb-8">
        <span className="text-sm font-medium">Tags (up to {MAX_TAGS})</span>
        <div className="flex flex-wrap gap-2 mt-1.5 mb-2">
          {tags.map((t) => (
            <span key={t} className="pb-chip">
              #{t}
              <button className="pb-chip-remove" onClick={() => setTags(tags.filter((x) => x !== t))} aria-label={`Remove ${t}`}>
                ×
              </button>
            </span>
          ))}
        </div>
        {tags.length < MAX_TAGS && (
          <div className="relative">
            <form onSubmit={(e) => { e.preventDefault(); addTag() }} className="flex gap-2">
              <input
                className="pb-input flex-1"
                placeholder="e.g. gaming, anime, coding"
                value={tagInput}
                onChange={(e) => setTagInput(e.target.value)}
                autoComplete="off"
              />
              <button type="submit" className="pb-btn-secondary">Add</button>
            </form>
            {tagInput.trim() && (() => {
              const q = tagInput.trim().replace(/^#/, '').toLowerCase()
              const matches = TAG_SUGGESTIONS.filter((s) => s.includes(q) && !tags.includes(s))
              if (matches.length === 0) return null
              return (
                <div
                  className="absolute z-10 mt-1 w-full rounded-lg border overflow-hidden"
                  style={{ background: 'rgba(13,16,24,0.97)', borderColor: 'var(--pb-border)' }}
                >
                  {matches.map((s) => (
                    <button
                      key={s}
                      type="button"
                      className="block w-full text-left px-3 py-2 text-sm hover:bg-white/5"
                      onClick={() => {
                        if (tags.length < MAX_TAGS) setTags([...tags, s])
                        setTagInput('')
                      }}
                    >
                      #{s}
                    </button>
                  ))}
                </div>
              )
            })()}
          </div>
        )}
      </label>

      <button className="pb-btn-primary" onClick={submit} disabled={saving || !inviteUrl.trim()}>
        {saving ? 'Verifying & saving…' : 'Publish listing'}
      </button>
      {saveError && <p className="text-sm mt-3" style={{ color: 'var(--pb-danger)' }}>{saveError}</p>}
      {saved && (
        <div className="mt-3">
          <p className="text-sm" style={{ color: 'var(--pb-positive)' }}>
            ✅ Saved — <a href="/servers" className="underline">view it on the directory</a>.
          </p>
          {refCode && (
            <p className="text-xs mt-2" style={{ color: 'var(--pb-text-faint)' }}>
              Your boost link (share it — joins through it raise your ranking):{' '}
              <code className="pb-code">{`/servers?ref=${refCode}`}</code>
            </p>
          )}
        </div>
      )}

      <p className="text-xs mt-10" style={{ color: 'var(--pb-text-faint)' }}>
        This link edits your listing — don't share it outside your admin team. Rerun{' '}
        <code className="pb-code">/setup servers</code> in Discord any time to get it again.
      </p>
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="relative px-6 py-12 min-h-screen overflow-hidden">
      <Image
        src="/submit-hero.png"
        alt=""
        fill
        priority
        sizes="100vw"
        className="object-cover -z-20"
        style={{ objectPosition: '20% center' }}
      />
      <div
        className="fixed inset-0 -z-10"
        style={{ background: 'linear-gradient(180deg, rgba(5,6,10,0.55) 0%, rgba(5,6,10,0.8) 35%, rgba(5,6,10,0.92) 100%)' }}
      />
      <div className="relative max-w-xl mx-auto">{children}</div>
    </main>
  )
}
