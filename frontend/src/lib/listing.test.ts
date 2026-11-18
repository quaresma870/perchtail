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
    expect(listing).toEqual({ entries: [], truncated: true, limit: 5000, cutBy: null })
  })

  it('reads who cut the listing', () => {
    const headers = new Headers({ 'x-listing-truncated': '1', 'x-listing-limit': '5000', 'x-listing-cut-by': 'source' })
    expect(readListing([], headers).cutBy).toBe('source')
    headers.set('x-listing-cut-by', 'archive')
    expect(readListing([], headers).cutBy).toBe('archive')
    headers.set('x-listing-cut-by', 'something else')
    expect(readListing([], headers).cutBy).toBeNull()
  })

  it('is not truncated without the header', () => {
    expect(readListing([], new Headers())).toEqual({ entries: [], truncated: false, limit: null, cutBy: null })
  })
})

describe('truncatedNotice', () => {
  it('says how many are shown and what to do when the cap cut it', () => {
    expect(truncatedNotice({ entries: [], truncated: true, limit: 5000, cutBy: null })).toBe(
      'Showing the first 5,000 entries; narrow with the search.',
    )
  })

  it("says an archive cut by its own reader has more entries than were read, not the explorer's cap", () => {
    const entries = [entry('x.tar.gz/a.log'), entry('x.tar.gz/b.log')]
    const notice = truncatedNotice({ entries, truncated: true, limit: 100_000, cutBy: 'archive' })
    expect(notice).not.toContain('100,000')
    expect(notice).toBe('This archive has more entries than were read; showing the 2 read.')
  })

  it("says a folder the source cut (an agent's 50,000) is what the source listed, not the cap", () => {
    const entries = [entry('a.log'), entry('b.log'), entry('c.log')]
    const notice = truncatedNotice({ entries, truncated: true, limit: 5000, cutBy: 'source' })
    expect(notice).not.toContain('5,000')
    expect(notice).toBe('The source listed only part of this folder; showing the 3 entries it listed.')
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
