import type { Source } from './types'

// Connections-home search box (ROADMAP.md's "Connections home redesign"):
// matches folder, customer, or host, case-insensitive -- deliberately not
// the source's own display name, per spec.
export function filterConnections(sources: Source[], query: string): Source[] {
  const q = query.trim().toLowerCase()
  if (!q) return sources
  return sources.filter(
    (s) =>
      (s.folder_name?.toLowerCase().includes(q) ?? false) ||
      (s.customer_name?.toLowerCase().includes(q) ?? false) ||
      s.host.toLowerCase().includes(q),
  )
}
