import { describe, expect, it } from 'vitest'
import {
  capTailBuffer,
  countNewlines,
  dropPartialFirstLine,
  isLargeFile,
  LARGE_FILE_BYTES,
  MAX_OPEN_TABS,
  shiftBookmarks,
  tabLimitMessage,
  tabLimitReached,
  TAIL_MAX_LINES,
  tailStartOffset,
} from './viewer-limits'

describe('the open tab cap', () => {
  it('allows up to MAX_OPEN_TABS tabs', () => {
    expect(MAX_OPEN_TABS).toBe(30)
    expect(tabLimitReached(29)).toBe(false)
    expect(tabLimitReached(30)).toBe(true)
    expect(tabLimitReached(2, 2)).toBe(true)
  })

  it('says how many and what to do', () => {
    expect(tabLimitMessage()).toBe('30 files are open, the most the viewer keeps at once. Close a tab to open another.')
  })
})

describe('the large file warning', () => {
  it('starts above 200 MB', () => {
    expect(LARGE_FILE_BYTES).toBe(200 * 1024 * 1024)
    expect(isLargeFile(LARGE_FILE_BYTES)).toBe(false)
    expect(isLargeFile(LARGE_FILE_BYTES + 1)).toBe(true)
    expect(isLargeFile(5, 4)).toBe(true)
  })

  it("doesn't warn when the size isn't known", () => {
    expect(isLargeFile(undefined)).toBe(false)
    expect(isLargeFile(null)).toBe(false)
    expect(isLargeFile(Number.NaN)).toBe(false)
  })
})

describe('the Tail buffer cap', () => {
  it('counts newlines', () => {
    expect(countNewlines('')).toBe(0)
    expect(countNewlines('a')).toBe(0)
    expect(countNewlines('a\nb\n')).toBe(2)
  })

  it('keeps everything up to the cap', () => {
    expect(capTailBuffer('a\nb\n', 2, 2)).toEqual({ content: 'a\nb\n', newlines: 2, dropped: 0 })
  })

  it('drops whole lines from the top past the cap', () => {
    expect(capTailBuffer('1\n2\n3\n4\n5', 4, 2)).toEqual({ content: '3\n4\n5', newlines: 2, dropped: 2 })
    expect(capTailBuffer('1\n2\n3\n', 3, 1)).toEqual({ content: '3\n', newlines: 1, dropped: 2 })
  })

  it('keeps the content when the count was wrong', () => {
    expect(capTailBuffer('1\n2', 5, 1)).toEqual({ content: '1\n2', newlines: 5, dropped: 0 })
  })

  it('defaults to 200,000 lines', () => {
    expect(TAIL_MAX_LINES).toBe(200_000)
    const content = 'x\n'.repeat(TAIL_MAX_LINES + 10)
    const capped = capTailBuffer(content, TAIL_MAX_LINES + 10)
    expect(capped.dropped).toBe(10)
    expect(countNewlines(capped.content)).toBe(TAIL_MAX_LINES)
  })

  it('moves bookmarks up with the dropped lines', () => {
    expect(shiftBookmarks([1, 3, 10], 2)).toEqual([1, 8])
    expect(shiftBookmarks([1, 3], 0)).toEqual([1, 3])
  })
})

describe('opening the end of a large file', () => {
  it('starts one follow answer before the end', () => {
    expect(tailStartOffset(10 * 1024 * 1024)).toBe(9 * 1024 * 1024)
    expect(tailStartOffset(100)).toBe(0)
  })

  it('drops the cut first line unless it read from the start', () => {
    expect(dropPartialFirstLine('tial line\nnext\n', 50)).toBe('next\n')
    expect(dropPartialFirstLine('whole\nnext\n', 0)).toBe('whole\nnext\n')
    expect(dropPartialFirstLine('no newline at all', 50)).toBe('no newline at all')
  })
})
