'use client'

import { useMemo, useState } from 'react'
import { MANUAL_SECTIONS, QUICK_REFERENCE, type Row } from './data'

function matches(row: Row, q: string) {
  if (!q) return true
  const hay = `${row.cmd} ${row.syntax} ${row.desc} ${row.perm}`.toLowerCase()
  return hay.includes(q.toLowerCase())
}

function Table({ rows }: { rows: Row[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 bg-neutral-900/60 text-left text-neutral-400">
            <th className="px-3 py-2 font-medium">Command</th>
            <th className="px-3 py-2 font-medium">Syntax</th>
            <th className="px-3 py-2 font-medium">What it does</th>
            <th className="px-3 py-2 font-medium">Permission</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-neutral-900 last:border-0">
              <td className="px-3 py-2 align-top">
                <code className="rounded bg-neutral-900 px-1.5 py-0.5 text-xs text-neutral-200">{r.cmd}</code>
              </td>
              <td className="px-3 py-2 align-top">
                <code className="text-xs text-neutral-400">{r.syntax}</code>
              </td>
              <td className="px-3 py-2 align-top text-neutral-200">{r.desc}</td>
              <td className="px-3 py-2 align-top whitespace-nowrap text-neutral-400">{r.perm}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ManualPage() {
  const [query, setQuery] = useState('')

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
    <main className="min-h-screen bg-neutral-950 px-6 py-12 text-neutral-100">
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">PRIME-BOT \u2014 Complete Command Manual</h1>
        <p className="mt-2 text-sm text-neutral-400">
          Every slash command in the bot, grouped by system, with exact syntax, what each option means, and who&apos;s
          allowed to use it. &quot;Anyone&quot; means no special permission is required. Where a permission is listed,
          Discord itself blocks anyone without it \u2014 the bot won&apos;t even let the command run.
        </p>

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <input
            className="input w-full max-w-sm"
            placeholder="Search commands\u2026"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <a href="/manual.pdf" className="btn-secondary" download>
            Download PDF
          </a>
        </div>

        {!query && (
          <div className="mt-8 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
            <h2 className="text-base font-medium">{QUICK_REFERENCE.title}</h2>
            <ul className="mt-3 space-y-2 text-sm text-neutral-300">
              {QUICK_REFERENCE.items.map((item, i) => (
                <li key={i}>
                  <span className="font-medium text-neutral-100">{item.label}</span>{' '}
                  <code className="rounded bg-neutral-900 px-1.5 py-0.5 text-xs">{item.code}</code>
                  {item.extra ? <span className="text-neutral-400"> {item.extra}</span> : null}
                </li>
              ))}
            </ul>
            <p className="mt-3 text-xs text-neutral-500">{QUICK_REFERENCE.footer}</p>
          </div>
        )}

        <nav className="mt-8 flex flex-wrap gap-2 text-xs">
          {MANUAL_SECTIONS.map((s) => (
            <a key={s.id} href={`#${s.id}`} className="btn-secondary !px-3 !py-1">
              {s.title}
            </a>
          ))}
        </nav>

        <div className="mt-8 space-y-12">
          {filtered.map((section) => (
            <section key={section.id} id={section.id}>
              <h2 className="text-lg font-semibold">{section.title}</h2>
              {section.intro && <p className="mt-1 text-sm text-neutral-400">{section.intro}</p>}
              {section.rows.length > 0 && (
                <div className="mt-4">
                  <Table rows={section.rows} />
                </div>
              )}
              {section.subs.map((sub, i) => (
                <div key={i} className="mt-6">
                  <h3 className="text-sm font-medium text-neutral-200">{sub.title}</h3>
                  {sub.note && <p className="mt-1 text-xs text-neutral-400">{sub.note}</p>}
                  <div className="mt-2">
                    <Table rows={sub.rows} />
                  </div>
                  {sub.footnote && <p className="mt-2 text-xs text-neutral-500">{sub.footnote}</p>}
                </div>
              ))}
            </section>
          ))}
          {filtered.length === 0 && <p className="text-sm text-neutral-500">No commands match &quot;{query}&quot;.</p>}
        </div>

        <p className="mt-12 text-xs text-neutral-600">
          Nothing above is invented \u2014 this reflects the bot&apos;s actual command set and current behavior
          (including how in-server setup wizards like /setupverification work). If a command you expected isn&apos;t
          listed, it doesn&apos;t exist in this build.
        </p>
      </div>

      <style jsx global>{`
        .input {
          background: #171717;
          border: 1px solid #333;
          border-radius: 6px;
          padding: 6px 10px;
          font-size: 14px;
          color: inherit;
        }
        .btn-secondary {
          display: inline-block;
          background: #262626;
          border: 1px solid #404040;
          border-radius: 6px;
          padding: 6px 14px;
          font-size: 14px;
          cursor: pointer;
          color: inherit;
          text-decoration: none;
        }
        .btn-secondary:hover {
          background: #333;
        }
      `}</style>
    </main>
  )
}
