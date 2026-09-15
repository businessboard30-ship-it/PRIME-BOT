'use client'

import { useEffect, useState } from 'react'
import { useParams, useSearchParams } from 'next/navigation'

type AutomodConfig = {
  action: string
  timeout_minutes: number
  log_channel_id: string | null
  word_filter_enabled: boolean
  banned_words: string[]
  anti_invite_enabled: boolean
  anti_mention_enabled: boolean
  anti_mention_threshold: number
  spam_enabled: boolean
  spam_flood_threshold: number
  spam_flood_window_seconds: number
  min_account_age_hours: number
}

const API_BASE = process.env.NEXT_PUBLIC_BOT_API_BASE || ''

export default function DashboardPage() {
  const params = useParams<{ guildId: string }>()
  const searchParams = useSearchParams()
  const token = searchParams.get('token') || ''

  const [config, setConfig] = useState<AutomodConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [newWord, setNewWord] = useState('')

  useEffect(() => {
    if (!token) {
      setError('Missing token — use the link from /automod dashboard in Discord.')
      setLoading(false)
      return
    }
    fetch(`${API_BASE}/api/discord_dashboard?guild_id=${params.guildId}&token=${encodeURIComponent(token)}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== 'ok') {
          setError(data.message || 'Could not load config')
        } else {
          setConfig(data.config)
        }
      })
      .catch(() => setError('Network error loading config'))
      .finally(() => setLoading(false))
  }, [params.guildId, token])

  async function save(patch: Partial<AutomodConfig>) {
    if (!config) return
    const next = { ...config, ...patch }
    setConfig(next)
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(
        `${API_BASE}/api/discord_dashboard?guild_id=${params.guildId}&token=${encodeURIComponent(token)}`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) }
      )
      const data = await res.json()
      if (data.status !== 'ok') {
        setError(data.message || 'Save failed')
      } else {
        setConfig(data.config)
        setSavedAt(Date.now())
      }
    } catch {
      setError('Network error saving changes')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return <Shell><p className="text-sm" style={{ color: 'var(--pb-text-muted)' }}>Loading…</p></Shell>
  }
  if (error && !config) {
    return <Shell><p className="text-sm" style={{ color: 'var(--pb-danger)' }}>{error}</p></Shell>
  }
  if (!config) return null

  return (
    <Shell>
      <div className="flex items-baseline justify-between mb-8">
        <h1 className="pb-heading text-2xl font-semibold">Auto-moderation</h1>
        <SaveStatus saving={saving} savedAt={savedAt} error={error} />
      </div>

      <Section title="What happens on a violation">
        <Row label="Action">
          <select
            className="pb-select"
            value={config.action}
            onChange={(e) => save({ action: e.target.value })}
          >
            <option value="delete">Delete message</option>
            <option value="warn">Delete + warn</option>
            <option value="timeout">Delete + timeout</option>
            <option value="kick">Delete + kick</option>
          </select>
        </Row>
        {config.action === 'timeout' && (
          <Row label="Timeout length (minutes)">
            <NumberInput value={config.timeout_minutes} onCommit={(v) => save({ timeout_minutes: v })} min={1} />
          </Row>
        )}
      </Section>

      <Section title="Filters">
        <Toggle
          label="Word filter"
          checked={config.word_filter_enabled}
          onChange={(v) => save({ word_filter_enabled: v })}
        />
        {config.word_filter_enabled && (
          <div className="ml-4 mb-4">
            <div className="flex flex-wrap gap-2 mb-2">
              {config.banned_words.length === 0 && (
                <span className="text-sm" style={{ color: 'var(--pb-text-faint)' }}>No words blocked yet.</span>
              )}
              {config.banned_words.map((w) => (
                <span key={w} className="pb-chip">
                  {w}
                  <button
                    className="pb-chip-remove"
                    onClick={() => save({ banned_words: config.banned_words.filter((x) => x !== w) })}
                    aria-label={`Remove ${w}`}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                const w = newWord.trim().toLowerCase()
                if (!w || config.banned_words.includes(w)) return
                save({ banned_words: [...config.banned_words, w] })
                setNewWord('')
              }}
              className="flex gap-2"
            >
              <input
                className="pb-input flex-1"
                placeholder="Add a blocked word or phrase"
                value={newWord}
                onChange={(e) => setNewWord(e.target.value)}
              />
              <button type="submit" className="pb-btn-secondary">Add</button>
            </form>
          </div>
        )}

        <Toggle
          label="Invite link filter"
          checked={config.anti_invite_enabled}
          onChange={(v) => save({ anti_invite_enabled: v })}
        />

        <Toggle
          label="Mass-mention filter"
          checked={config.anti_mention_enabled}
          onChange={(v) => save({ anti_mention_enabled: v })}
        />
        {config.anti_mention_enabled && (
          <Row label="Mentions in one message that trigger it">
            <NumberInput value={config.anti_mention_threshold} onCommit={(v) => save({ anti_mention_threshold: v })} min={1} />
          </Row>
        )}

        <Toggle
          label="Spam / flood filter"
          checked={config.spam_enabled}
          onChange={(v) => save({ spam_enabled: v })}
        />
        {config.spam_enabled && (
          <>
            <Row label="Messages allowed">
              <NumberInput value={config.spam_flood_threshold} onCommit={(v) => save({ spam_flood_threshold: v })} min={1} />
            </Row>
            <Row label="Within (seconds)">
              <NumberInput value={config.spam_flood_window_seconds} onCommit={(v) => save({ spam_flood_window_seconds: v })} min={1} />
            </Row>
          </>
        )}
      </Section>

      <Section title="Raid protection">
        <Row label="Kick new joiners younger than (hours, 0 = off)">
          <NumberInput value={config.min_account_age_hours} onCommit={(v) => save({ min_account_age_hours: v })} min={0} />
        </Row>
      </Section>

      <p className="text-xs mt-10" style={{ color: 'var(--pb-text-faint)' }}>
        Changes save automatically. This link grants full access to this server's auto-mod config —
        don't share it outside your admin team.
      </p>
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="pb-page px-6 py-12">
      <div className="max-w-xl mx-auto">{children}</div>
    </main>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-8 pb-8 border-b last:border-0" style={{ borderColor: 'var(--pb-line)' }}>
      <h2 className="pb-heading text-sm font-medium mb-4" style={{ color: 'var(--pb-text-muted)' }}>
        {title}
      </h2>
      {children}
    </section>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-2">
      <span className="text-sm">{label}</span>
      {children}
    </div>
  )
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-center justify-between py-2">
      <span className="text-sm">{label}</span>
      <button role="switch" aria-checked={checked} onClick={() => onChange(!checked)} className="pb-toggle">
        <span className="pb-toggle-knob" />
      </button>
    </div>
  )
}

function NumberInput({ value, onCommit, min }: { value: number; onCommit: (v: number) => void; min: number }) {
  const [local, setLocal] = useState(String(value))
  useEffect(() => setLocal(String(value)), [value])
  return (
    <input
      className="pb-select w-20 text-right"
      type="number"
      min={min}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => {
        const n = Math.max(min, parseInt(local || String(min), 10) || min)
        onCommit(n)
      }}
    />
  )
}

function SaveStatus({ saving, savedAt, error }: { saving: boolean; savedAt: number | null; error: string | null }) {
  if (error) return <span className="text-xs" style={{ color: 'var(--pb-danger)' }}>{error}</span>
  if (saving) return <span className="text-xs" style={{ color: 'var(--pb-text-faint)' }}>Saving…</span>
  if (savedAt) return <span className="text-xs" style={{ color: 'var(--pb-positive)' }}>Saved</span>
  return null
}
