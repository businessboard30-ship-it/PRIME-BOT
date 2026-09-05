'use client'

import { useEffect, useMemo, useState } from 'react'
import { MANUAL_SECTIONS, QUICK_REFERENCE, type Row } from './data'

const THEMES = {
  midnight: { label: 'Midnight', swatch: '#171717' },
  light: { label: 'Light', swatch: '#f5f5f5' },
  ocean: { label: 'Ocean', swatch: '#0b2942' },
  sunset: { label: 'Sunset', swatch: '#2a1810' },
} as const

type ThemeId = keyof typeof THEMES
const DEFAULT_THEME: ThemeId = 'midnight'
const STORAGE_KEY = 'prime-manual-theme'

function useManualTheme() {
  const [theme, setTheme] = useState<ThemeId>(DEFAULT_THEME)

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY)
    if (saved && saved in THEMES) setTheme(saved as ThemeId)
  }, [])

  const update = (t: ThemeId) => {
    setTheme(t)
    window.localStorage.setItem(STORAGE_KEY, t)
  }

  return [theme, update] as const
}

function matches(row: Row, q: string) {
  if (!q) return true
  const hay = `${row.cmd} ${row.syntax} ${row.desc} ${row.perm}`.toLowerCase()
  return hay.includes(q.toLowerCase())
}

function Table({ rows }: { rows: Row[] }) {
  return (
    <div className="manual-panel overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead>
          <tr className="manual-thead border-b text-left">
            <th className="px-3 py-2 font-medium">Command</th>
            <th className="px-3 py-2 font-medium">Syntax</th>
            <th className="px-3 py-2 font-medium">What it does</th>
            <th className="px-3 py-2 font-medium">Permission</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="manual-row border-b last:border-0">
              <td className="px-3 py-2 align-top">
                <code className="manual-code rounded px-1.5 py-0.5 text-xs">{r.cmd}</code>
              </td>
              <td className="px-3 py-2 align-top">
                <code className="manual-muted text-xs">{r.syntax}</code>
              </td>
              <td className="manual-fg px-3 py-2 align-top">{r.desc}</td>
              <td className="manual-muted px-3 py-2 align-top whitespace-nowrap">{r.perm}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ThemePicker({ theme, onChange }: { theme: ThemeId; onChange: (t: ThemeId) => void }) {
  return (
    <div className="flex items-center gap-1.5">
      {(Object.keys(THEMES) as ThemeId[]).map((id) => (
        <button
          key={id}
          type="button"
          title={THEMES[id].label}
          aria-label={`${THEMES[id].label} theme`}
          onClick={() => onChange(id)}
          className="h-6 w-6 rounded-full border-2 transition-transform hover:scale-110"
          style={{
            background: THEMES[id].swatch,
            borderColor: theme === id ? 'var(--manual-accent)' : 'var(--manual-border)',
          }}
        />
      ))}
    </div>
  )
}

export default function ManualPage() {
  const [query, setQuery] = useState('')
  const [theme, setTheme] = useManualTheme()

  const filtered = useMemo(() => {
    const q = query.trim()
    return MANUAL_SECTIONS.map((section) => {
      const rows = (section.rows || []).filter((r) => matches(r, q))
      const subs = (section.subs || [])
        .map((sub) => ({ ...sub, rows: sub.rows.filter((r) => matches(r, q)) }))
        .filter((sub) => sub.rows.length > 0)
      return { ...section, rows, subs }
    }).filter((section) => section.rows.length > 0 || section.subs.length > 0)
  }, [query])

  return (
    <main data-manual-theme={theme} className="manual-root min-h-screen px-6 py-12">
      <div className="mx-auto max-w-4xl">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">PRIME-BOT \u2014 Complete Command Manual</h1>
            <p className="manual-muted mt-2 text-sm">
              Every slash command in the bot, grouped by system, with exact syntax, what each option means, and
              who&apos;s allowed to use it. &quot;Anyone&quot; means no special permission is required. Where a
              permission is listed, Discord itself blocks anyone without it \u2014 the bot won&apos;t even let the
              command run.
            </p>
          </div>
          <ThemePicker theme={theme} onChange={setTheme} />
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <input
            className="manual-input w-full max-w-sm"
            placeholder="Search commands\u2026"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <a href="/manual.pdf" className="manual-btn" download>
            Download PDF
          </a>
        </div>

        {!query && (
          <div className="manual-panel mt-8 rounded-lg border p-4">
            <h2 className="text-base font-medium">{QUICK_REFERENCE.title}</h2>
            <ul className="manual-fg mt-3 space-y-2 text-sm">
              {QUICK_REFERENCE.items.map((item, i) => (
                <li key={i}>
                  <span className="font-medium">{item.label}</span>{' '}
                  <code className="manual-code rounded px-1.5 py-0.5 text-xs">{item.code}</code>
                  {item.extra ? <span className="manual-muted"> {item.extra}</span> : null}
                </li>
              ))}
            </ul>
            <p className="manual-muted mt-3 text-xs">{QUICK_REFERENCE.footer}</p>
          </div>
        )}

        <nav className="mt-8 flex flex-wrap gap-2 text-xs">
          {MANUAL_SECTIONS.map((s) => (
            <a key={s.id} href={`#${s.id}`} className="manual-btn !px-3 !py-1">
              {s.title}
            </a>
          ))}
        </nav>

        <div className="mt-8 space-y-12">
          {filtered.map((section) => (
            <section key={section.id} id={section.id}>
              <h2 className="text-lg font-semibold">{section.title}</h2>
              {section.intro && <p className="manual-muted mt-1 text-sm">{section.intro}</p>}
              {section.rows.length > 0 && (
                <div className="mt-4">
                  <Table rows={section.rows} />
                </div>
              )}
              {section.subs.map((sub, i) => (
                <div key={i} className="mt-6">
                  <h3 className="manual-fg text-sm font-medium">{sub.title}</h3>
                  {sub.note && <p className="manual-muted mt-1 text-xs">{sub.note}</p>}
                  <div className="mt-2">
                    <Table rows={sub.rows} />
                  </div>
                  {sub.footnote && <p className="manual-muted mt-2 text-xs">{sub.footnote}</p>}
                </div>
              ))}
            </section>
          ))}
          {filtered.length === 0 && <p className="manual-muted text-sm">No commands match &quot;{query}&quot;.</p>}
        </div>

        <p className="manual-faint mt-12 text-xs">
          Nothing above is invented \u2014 this reflects the bot&apos;s actual command set and current behavior
          (including how in-server setup wizards like /setupverification work). If a command you expected isn&apos;t
          listed, it doesn&apos;t exist in this build.
        </p>
      </div>

      <style jsx global>{`
        .manual-root {
          background: var(--manual-bg);
          color: var(--manual-fg);
        }
        [data-manual-theme='midnight'] {
          --manual-bg: #0a0a0a;
          --manual-fg: #f5f5f5;
          --manual-muted: #a3a3a3;
          --manual-faint: #737373;
          --manual-border: #262626;
          --manual-panel: rgba(38, 38, 38, 0.4);
          --manual-code-bg: #171717;
          --manual-btn-bg: #262626;
          --manual-btn-border: #404040;
          --manual-btn-hover: #333333;
          --manual-accent: #60a5fa;
        }
        [data-manual-theme='light'] {
          --manual-bg: #fafafa;
          --manual-fg: #171717;
          --manual-muted: #525252;
          --manual-faint: #a3a3a3;
          --manual-border: #e5e5e5;
          --manual-panel: rgba(229, 229, 229, 0.5);
          --manual-code-bg: #eeeeee;
          --manual-btn-bg: #ececec;
          --manual-btn-border: #d4d4d4;
          --manual-btn-hover: #e0e0e0;
          --manual-accent: #2563eb;
        }
        [data-manual-theme='ocean'] {
          --manual-bg: #051624;
          --manual-fg: #e0f2fe;
          --manual-muted: #7dd3fc;
          --manual-faint: #38618a;
          --manual-border: #123a54;
          --manual-panel: rgba(18, 58, 84, 0.5);
          --manual-code-bg: #0b2942;
          --manual-btn-bg: #0e3350;
          --manual-btn-border: #1c5177;
          --manual-btn-hover: #144163;
          --manual-accent: #38bdf8;
        }
        [data-manual-theme='sunset'] {
          --manual-bg: #1a0f0a;
          --manual-fg: #fde8d7;
          --manual-muted: #d6a377;
          --manual-faint: #8a5f3f;
          --manual-border: #3a2314;
          --manual-panel: rgba(58, 35, 20, 0.5);
          --manual-code-bg: #2a1810;
          --manual-btn-bg: #331d10;
          --manual-btn-border: #5c3820;
          --manual-btn-hover: #46280e;
          --manual-accent: #fb923c;
        }
        .manual-fg {
          color: var(--manual-fg);
        }
        .manual-muted {
          color: var(--manual-muted);
        }
        .manual-faint {
          color: var(--manual-faint);
        }
        .manual-panel {
          background: var(--manual-panel);
          border-color: var(--manual-border);
        }
        .manual-thead {
          background: var(--manual-panel);
          border-color: var(--manual-border);
          color: var(--manual-muted);
        }
        .manual-row {
          border-color: var(--manual-border);
        }
        .manual-code {
          background: var(--manual-code-bg);
          color: var(--manual-fg);
        }
        .manual-input {
          background: var(--manual-code-bg);
          border: 1px solid var(--manual-btn-border);
          border-radius: 6px;
          padding: 6px 10px;
          font-size: 14px;
          color: var(--manual-fg);
        }
        .manual-btn {
          display: inline-block;
          background: var(--manual-btn-bg);
          border: 1px solid var(--manual-btn-border);
          border-radius: 6px;
          padding: 6px 14px;
          font-size: 14px;
          cursor: pointer;
          color: var(--manual-fg);
          text-decoration: none;
        }
        .manual-btn:hover {
          background: var(--manual-btn-hover);
        }
      `}</style>
    </main>
  )
}
