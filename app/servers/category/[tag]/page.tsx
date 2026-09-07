// path: app/servers/category/[tag]/page.tsx

import ServerDirectory from '../../_ServerDirectory'

// Thin wrapper around the same directory component the homepage and
// /servers use — passes its tag through as the initial filter so a
// category link (e.g. from a "browse by category" nav, or shared directly)
// lands pre-filtered instead of on the unfiltered full list.
export default async function CategoryPage({ params }: { params: Promise<{ tag: string }> }) {
  const { tag: rawTag } = await params
  const tag = decodeURIComponent(rawTag).toLowerCase()
  return (
    <main className="pb-page px-6 py-12">
      <div className="max-w-2xl mx-auto">
        <ServerDirectory initialTag={tag} />
      </div>
    </main>
  )
}
