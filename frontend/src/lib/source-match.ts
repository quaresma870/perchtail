import type { Source } from './types'

// Search page's "sources matching" section (ROADMAP.md's full-text search
// path/host matching notes): a source or file whose name/host matches the
// query should surface even if none of its indexed lines happen to contain
// that text. Deliberately client-side over the already-fetched sources list
// rather than a backend query -- it's metadata, not indexed log content, so
// there's no staleness/re-index lag to design around, and the Search page
// already loads every visible source to resolve hit.source_id to a name.
//
// Matches name or host, case-insensitive -- distinct from
// connection-filter.ts's filterConnections (folder/customer/host, not name),
// which serves the Viewer home page's different "browse by org unit" intent.
export function filterSourcesByNameOrHost(sources: Source[], query: string): Source[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  return sources.filter(
    (s) => s.name.toLowerCase().includes(q) || s.host.toLowerCase().includes(q),
  )
}
