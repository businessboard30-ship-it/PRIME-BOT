// path: app/servers/page.tsx

'use client'

// Thin wrapper — the actual directory (listings, search, "list your
// server" instructions) lives in ./_ServerDirectory.tsx so app/page.tsx
// (the homepage) can render the identical content without duplicating it.
import ServerDirectory from './_ServerDirectory'

export default function ServersPage() {
  return (
    <main className="pb-page px-6 py-12">
      <div className="max-w-2xl mx-auto">
        <ServerDirectory />
      </div>
    </main>
  )
}
