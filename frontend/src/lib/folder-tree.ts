import type { Folder } from './types'

function groupByParent(folders: Folder[]): Map<number | null, Folder[]> {
  const byParent = new Map<number | null, Folder[]>()
  for (const f of folders) {
    const key = f.parent_folder_id
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key)!.push(f)
  }
  for (const list of byParent.values()) list.sort((a, b) => a.name.localeCompare(b.name))
  return byParent
}

export interface FlatFolderOption {
  folder: Folder
  depth: number
}

/** Folders come back from the API flat; nest them depth-first so a
 * `<select>` can show hierarchy via indentation for an arbitrarily deep
 * tree. Originally SourceEditor.svelte's own `folderOptions`, generalized
 * here so the admin Folders management page can reuse the exact same
 * ordering/grouping logic instead of a second implementation. */
export function nestFoldersFlat(folders: Folder[]): FlatFolderOption[] {
  const byParent = groupByParent(folders)
  const result: FlatFolderOption[] = []
  function walk(parentId: number | null, depth: number) {
    for (const f of byParent.get(parentId) ?? []) {
      result.push({ folder: f, depth })
      walk(f.id, depth + 1)
    }
  }
  walk(null, 0)
  return result
}

export interface FolderTreeNode {
  folder: Folder
  children: FolderTreeNode[]
}

/** Same grouping as `nestFoldersFlat`, shaped as a real nested tree
 * instead of a flat depth-annotated list -- for the admin Folders page's
 * expandable tree UI, which needs each node's children as its own array
 * to recurse into (`<svelte:self>`), not just an indentation hint. */
export function nestFoldersTree(folders: Folder[]): FolderTreeNode[] {
  const byParent = groupByParent(folders)
  function build(parentId: number | null): FolderTreeNode[] {
    return (byParent.get(parentId) ?? []).map((folder) => ({
      folder,
      children: build(folder.id),
    }))
  }
  return build(null)
}
