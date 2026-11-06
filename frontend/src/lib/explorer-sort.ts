// How the explorer orders a folder (#273). Folders always come first; the
// order inside each group is the user's choice (or the System default).
import type { BrowseEntry, ExplorerSort } from './types'

export const EXPLORER_SORTS: { value: ExplorerSort; label: string }[] = [
  { value: 'name_asc', label: 'Name A→Z' },
  { value: 'name_desc', label: 'Name Z→A' },
  { value: 'mtime_desc', label: 'Newest first' },
  { value: 'mtime_asc', label: 'Oldest first' },
  { value: 'size_desc', label: 'Largest first' },
]

const byName = (a: BrowseEntry, b: BrowseEntry) =>
  a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })

// Entries without a time (older agents) go last whichever way time sorts.
function byTime(a: BrowseEntry, b: BrowseEntry, newestFirst: boolean): number {
  const at = a.mtime ?? null
  const bt = b.mtime ?? null
  if (at === null || bt === null) return at === bt ? byName(a, b) : at === null ? 1 : -1
  return (newestFirst ? bt - at : at - bt) || byName(a, b)
}

export function sortEntries(entries: BrowseEntry[], mode: ExplorerSort): BrowseEntry[] {
  const compare = (a: BrowseEntry, b: BrowseEntry): number => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    switch (mode) {
      case 'name_desc':
        return byName(b, a)
      case 'mtime_desc':
        return byTime(a, b, true)
      case 'mtime_asc':
        return byTime(a, b, false)
      case 'size_desc':
        return b.size - a.size || byName(a, b)
      default:
        return byName(a, b)
    }
  }
  return [...entries].sort(compare)
}

/** A compact time for the tree: the hour today, the date otherwise. */
export function formatEntryTime(mtime: number | null | undefined, now = new Date()): string {
  if (mtime === null || mtime === undefined) return ''
  const when = new Date(mtime * 1000)
  const sameDay = when.toDateString() === now.toDateString()
  const pad = (n: number) => String(n).padStart(2, '0')
  if (sameDay) return `${pad(when.getHours())}:${pad(when.getMinutes())}`
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`
}
