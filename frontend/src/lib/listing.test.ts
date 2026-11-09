import { describe, expect, it } from 'vitest'
import { archiveChildren, archiveMembersUrl, readListing, truncatedNotice } from './listing'
import type { BrowseEntry } from './types'

const entry = (path: string, is_dir = false): BrowseEntry => ({
  name: path.split('/').pop() ?? path,
  path,
  is_dir,
  size: 1,
  is_archive: false,
})

describe('readListing', () => {
  it('reads the cap headers', () => {
    const listing = readListing([], new Headers({ 'x-listing-truncated': '1', 'x-listing-limit': '5000' }))
    expect(listing).toEqual({ entries: [], truncated: true, limit: 5000 })
  })

  it('is not truncated without the header', () => {
    expect(readListing([], new Headers())).toEqual({ entries: [], truncated: false, limit: null })
  })
})

describe('truncatedNotice', () => {
  it('says how many are shown and what to do', () => {
    expect(truncatedNotice({ entries: [], truncated: true, limit: 5000 })).toBe(
      'Showing the first 5,000 entries; narrow with the search.',
    )
  })
})

describe('archive members', () => {
  const members = [
    entry('logs/x.zip/a', true),
    entry('logs/x.zip/a/b', true),
    entry('logs/x.zip/a/b/c.log'),
    entry('logs/x.zip/a/d.log'),
    entry('logs/x.zip/e.log'),
  ]

  it('lists every member at once', () => {
    expect(archiveMembersUrl(3, 'logs/x.zip')).toBe('/sources/3/browse?path=logs%2Fx.zip&all=true')
  })

  it('gives the children of the archive and of a folder in it', () => {
    expect(archiveChildren(members, 'logs/x.zip').map((m) => m.path)).toEqual(['logs/x.zip/a', 'logs/x.zip/e.log'])
    expect(archiveChildren(members, 'logs/x.zip/a').map((m) => m.path)).toEqual([
      'logs/x.zip/a/b',
      'logs/x.zip/a/d.log',
    ])
    expect(archiveChildren(members, 'logs/x.zip/a/b/c.log')).toEqual([])
  })
})
