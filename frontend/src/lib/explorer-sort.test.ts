import { describe, expect, it } from 'vitest'
import { formatEntryTime, sortEntries } from './explorer-sort'
import type { BrowseEntry } from './types'

const entry = (name: string, extra: Partial<BrowseEntry> = {}): BrowseEntry => ({
  name,
  path: name,
  is_dir: false,
  size: 0,
  is_archive: false,
  ...extra,
})

const entries = [
  entry('app-10.log', { mtime: 300, size: 5 }),
  entry('app-2.log', { mtime: 100, size: 50 }),
  entry('old.log', { mtime: null, size: 1 }),
  entry('zdir', { is_dir: true, mtime: 1 }),
  entry('adir', { is_dir: true, mtime: 999 }),
]
const names = (list: BrowseEntry[]) => list.map((e) => e.name)

describe('sortEntries', () => {
  it('keeps folders first and sorts names naturally', () => {
    expect(names(sortEntries(entries, 'name_asc'))).toEqual([
      'adir',
      'zdir',
      'app-2.log',
      'app-10.log',
      'old.log',
    ])
    expect(names(sortEntries(entries, 'name_desc'))).toEqual([
      'zdir',
      'adir',
      'old.log',
      'app-10.log',
      'app-2.log',
    ])
  })

  it('sorts by time either way, with unknown times last', () => {
    expect(names(sortEntries(entries, 'mtime_desc'))).toEqual([
      'adir',
      'zdir',
      'app-10.log',
      'app-2.log',
      'old.log',
    ])
    expect(names(sortEntries(entries, 'mtime_asc'))).toEqual([
      'zdir',
      'adir',
      'app-2.log',
      'app-10.log',
      'old.log',
    ])
  })

  it('sorts by size, largest first', () => {
    expect(names(sortEntries(entries, 'size_desc')).slice(2)).toEqual([
      'app-2.log',
      'app-10.log',
      'old.log',
    ])
  })

  it('does not reorder its input', () => {
    const copy = [...entries]
    sortEntries(entries, 'mtime_desc')
    expect(entries).toEqual(copy)
  })
})

describe('formatEntryTime', () => {
  it('shows the hour today and the date otherwise', () => {
    const now = new Date(2026, 10, 6, 15, 0)
    expect(formatEntryTime(new Date(2026, 10, 6, 9, 5).getTime() / 1000, now)).toBe('09:05')
    expect(formatEntryTime(new Date(2026, 10, 5, 9, 5).getTime() / 1000, now)).toBe('2026-11-05')
    expect(formatEntryTime(null, now)).toBe('')
  })
})
