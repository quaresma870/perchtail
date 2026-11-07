import type { BrowseEntry } from './types'

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

/** A file size for the explorer (1024-based, as file managers show it):
 * "0 B", "512 B", "1.5 KB", "34 KB", "2.3 GB". */
export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit++
  }
  if (unit === 0) return `${bytes} B`
  // One decimal below 10 ("1.5 MB"), whole numbers above ("34 MB"); a value
  // that rounds up to 1024 moves to the next unit.
  let shown = value < 10 ? Math.round(value * 10) / 10 : Math.round(value)
  if (shown >= 1024 && unit < UNITS.length - 1) {
    shown = 1
    unit++
  }
  return `${shown} ${UNITS[unit]}`
}

/** The hover text for a file: the exact size and, when the source says,
 * when it was last modified. */
export function fileDetails(entry: Pick<BrowseEntry, 'size' | 'mtime'>): string {
  const lines = [`Size: ${formatSize(entry.size)} (${entry.size.toLocaleString()} bytes)`]
  if (entry.mtime !== null && entry.mtime !== undefined) {
    lines.push(`Modified: ${new Date(entry.mtime * 1000).toLocaleString()}`)
  }
  return lines.join('\n')
}
