import { memberPath } from './tab-key'
import type { BrowseEntry } from './types'

/** Builds the download URL for a tree entry, or null if the entry can't be
 * downloaded directly (a plain directory nested inside an archive — the
 * backend only expands one level of archive, so there's nothing to zip).
 * Extracted from FolderTree.svelte so the three-way branch (plain folder /
 * plain file / file inside an archive) is unit-testable without mounting a
 * component. */
export function downloadHref(
  sourceId: number,
  entry: Pick<BrowseEntry, 'path' | 'is_dir' | 'is_archive'>,
  archiveRoot: string | null,
): string | null {
  if (entry.is_dir && !entry.is_archive && archiveRoot === null) {
    return `/sources/${sourceId}/download-zip?path=${encodeURIComponent(entry.path)}`
  }
  if (!entry.is_dir) {
    const params = new URLSearchParams({ path: archiveRoot ?? entry.path })
    if (archiveRoot !== null) {
      params.set('member', memberPath(entry.path, archiveRoot))
    }
    return `/sources/${sourceId}/download?${params.toString()}`
  }
  return null
}
