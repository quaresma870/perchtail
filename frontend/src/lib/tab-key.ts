/** Identifies an open tab / tree node: a plain file is keyed by its path with
 * an empty member; a file inside an archive is keyed by the archive's own
 * path plus the member name inside it. Shared by Viewer.svelte (which owns
 * the open tabs) and FolderTree.svelte (which highlights whichever tab is
 * active) so the two can never drift out of sync on the key format. */
export function tabKey(path: string, member: string | null): string {
  return `${path}::${member ?? ''}`
}
