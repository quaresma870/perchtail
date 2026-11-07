import { describe, expect, it } from 'vitest'
import { fileDetails, formatSize } from './file-details'

describe('formatSize', () => {
  it('shows bytes as they are', () => {
    expect(formatSize(0)).toBe('0 B')
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(1023)).toBe('1023 B')
  })

  it('uses one decimal below 10 and whole numbers above', () => {
    expect(formatSize(1024)).toBe('1 KB')
    expect(formatSize(1536)).toBe('1.5 KB')
    expect(formatSize(34 * 1024 + 300)).toBe('34 KB')
    expect(formatSize(5.25 * 1024 * 1024)).toBe('5.3 MB')
    expect(formatSize(109_539_638)).toBe('104 MB')
    expect(formatSize(2.3 * 1024 ** 3)).toBe('2.3 GB')
  })

  it('moves to the next unit when rounding reaches 1024', () => {
    expect(formatSize(1024 * 1024 - 1)).toBe('1 MB')
  })

  it('shows nothing for a size it cannot read', () => {
    expect(formatSize(-1)).toBe('')
    expect(formatSize(Number.NaN)).toBe('')
  })
})

describe('fileDetails', () => {
  it('gives the exact size and the modification time', () => {
    const text = fileDetails({ size: 1536, mtime: 1_700_000_000 })
    expect(text.split('\n')[0]).toBe(`Size: 1.5 KB (${(1536).toLocaleString()} bytes)`)
    expect(text.split('\n')[1]).toBe(`Modified: ${new Date(1_700_000_000_000).toLocaleString()}`)
  })

  it('leaves the time out when the source does not give one', () => {
    expect(fileDetails({ size: 10, mtime: null })).toBe('Size: 10 B (10 bytes)')
  })
})
