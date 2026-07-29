/** Identifies an open tab / tree node: a plain file is keyed by its path with
 * an empty member; a file inside an archive is keyed by the archive's own
 * path plus the member name inside it. Shared by Viewer.svelte (which owns
 * the open tabs) and FolderTree.svelte (which highlights whichever tab is
 * active) so the two can never drift out of sync on the key format. */
export function tabKey(path: string, member: string | null): string {
  return `${path}::${member ?? ''}`
}

/** The tree stores an archive member's full virtual path (e.g.
 * `/logs/app.zip/nested/error.log`), but the backend's open/download/close
 * endpoints want the archive's own path plus just the member's path *inside*
 * it (`nested/error.log`) — this strips the archive-root prefix to get that.
 * Pulled out because it was previously computed inline in three separate
 * places (FolderTree's key + open-dispatch logic, and download-href), any of
 * which could silently drift from the others. */
export function memberPath(entryPath: string, archiveRoot: string): string {
  return entryPath.slice(archiveRoot.length + 1)
}
