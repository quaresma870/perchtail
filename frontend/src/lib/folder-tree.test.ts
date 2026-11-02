import { describe, expect, it } from 'vitest'
import { nestFoldersFlat, nestFoldersTree } from './folder-tree'
import type { Folder } from './types'

function folder(overrides: Partial<Folder> & { id: number; name: string }): Folder {
  return { customer_id: 1, parent_folder_id: null, ...overrides }
}

describe('nestFoldersFlat', () => {
  it('walks each branch fully before moving to the next sibling (depth-first)', () => {
    const folders = [
      folder({ id: 1, name: 'Production' }),
      folder({ id: 2, name: 'App1', parent_folder_id: 1 }),
      folder({ id: 3, name: 'Staging' }),
    ]
    const result = nestFoldersFlat(folders)
    // Top-level siblings (Production, Staging) sorted alphabetically;
    // Production's child (App1) is visited immediately after Production,
    // before moving on to its sibling Staging.
    expect(result.map((r) => r.folder.name)).toEqual(['Production', 'App1', 'Staging'])
  })

  it('assigns increasing depth down each branch', () => {
    const folders = [
      folder({ id: 1, name: 'EU' }),
      folder({ id: 2, name: 'Production', parent_folder_id: 1 }),
      folder({ id: 3, name: 'App1', parent_folder_id: 2 }),
    ]
    const result = nestFoldersFlat(folders)
    expect(result).toEqual([
      { folder: folders[0], depth: 0 },
      { folder: folders[1], depth: 1 },
      { folder: folders[2], depth: 2 },
    ])
  })

  it('sorts siblings alphabetically at each level', () => {
    const folders = [
      folder({ id: 1, name: 'Zebra' }),
      folder({ id: 2, name: 'Alpha' }),
    ]
    expect(nestFoldersFlat(folders).map((r) => r.folder.name)).toEqual(['Alpha', 'Zebra'])
  })

  it('returns an empty list for no folders', () => {
    expect(nestFoldersFlat([])).toEqual([])
  })
})

describe('nestFoldersTree', () => {
  it('builds a real nested tree matching parent_folder_id', () => {
    const folders = [
      folder({ id: 1, name: 'EU' }),
      folder({ id: 2, name: 'US' }),
      folder({ id: 3, name: 'Production', parent_folder_id: 1 }),
      folder({ id: 4, name: 'App1', parent_folder_id: 3 }),
    ]
    const tree = nestFoldersTree(folders)
    expect(tree.map((n) => n.folder.name)).toEqual(['EU', 'US'])
    expect(tree[0].children.map((n) => n.folder.name)).toEqual(['Production'])
    expect(tree[0].children[0].children.map((n) => n.folder.name)).toEqual(['App1'])
    expect(tree[0].children[0].children[0].children).toEqual([])
    expect(tree[1].children).toEqual([])
  })

  it('returns an empty tree for no folders', () => {
    expect(nestFoldersTree([])).toEqual([])
  })
})
