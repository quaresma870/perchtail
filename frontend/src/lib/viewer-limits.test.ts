import { describe, expect, it } from 'vitest'
import {
  capTailBuffer,
  countNewlines,
  cutToFirstLine,
  dropPartialFirstLine,
  earlierWindow,
  loadEarlierBlocked,
  MAX_LOADED_EARLIER_BYTES,
  opensFromEnd,
  shiftBookmarksDown,
  startNotLoaded,
  TAIL_START_BYTES,
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

const MB = 1024 * 1024

describe('opening a file at its end', () => {
  it('opens a plain file over the threshold at its end', () => {
    expect(opensFromEnd(50 * MB + 1, 50, 'app.log', null)).toBe(true)
    expect(opensFromEnd(300 * MB, 50, 'app.log', null)).toBe(true)
  })

  it('opens a file at or under the threshold whole', () => {
    expect(opensFromEnd(50 * MB, 50, 'app.log', null)).toBe(false)
    expect(opensFromEnd(1000, 50, 'app.log', null)).toBe(false)
  })

  it('opens every file whole when the threshold is 0', () => {
    expect(opensFromEnd(300 * MB, 0, 'app.log', null)).toBe(false)
  })

  it('opens .gz files and archive members whole', () => {
    expect(opensFromEnd(300 * MB, 50, 'app.log.gz', null)).toBe(false)
    expect(opensFromEnd(300 * MB, 50, 'APP.LOG.GZ', null)).toBe(false)
    expect(opensFromEnd(300 * MB, 50, 'inner.log', 'logs/inner.log')).toBe(false)
  })

  it('opens whole when the size or the threshold is unknown', () => {
    expect(opensFromEnd(undefined, 50, 'app.log', null)).toBe(false)
    expect(opensFromEnd(null, 50, 'app.log', null)).toBe(false)
    expect(opensFromEnd(Number.NaN, 50, 'app.log', null)).toBe(false)
    expect(opensFromEnd(300 * MB, undefined, 'app.log', null)).toBe(false)
  })

  it('follows a lowered threshold', () => {
    expect(opensFromEnd(3 * MB, 1, 'app.log', null)).toBe(true)
  })
})

describe('Load earlier', () => {
  it('reads the window before the start, never before 0', () => {
    expect(earlierWindow(5 * MB)).toEqual({ offset: 5 * MB - TAIL_START_BYTES, end: 5 * MB })
    expect(earlierWindow(1000)).toEqual({ offset: 0, end: 1000 })
  })

  it('is blocked while Tail is on', () => {
    expect(loadEarlierBlocked({ following: true, byteSize: 10 * MB, startOffset: 9 * MB })).toBe(
      'Stop Tail to load earlier lines',
    )
  })

  it('is blocked once the tab holds MAX_LOADED_EARLIER_BYTES', () => {
    expect(MAX_LOADED_EARLIER_BYTES).toBe(32 * MB)
    const size = 300 * MB
    expect(loadEarlierBlocked({ following: false, byteSize: size, startOffset: size - 32 * MB + 1 })).toBe('')
    expect(loadEarlierBlocked({ following: false, byteSize: size, startOffset: size - 32 * MB })).toBe(
      'Load the whole file to see more',
    )
  })

  it('is blocked once Tail dropped lines from the top', () => {
    expect(
      loadEarlierBlocked({ following: false, byteSize: 10 * MB, startOffset: 9 * MB, droppedLines: 3 }),
    ).toBe('Load the whole file to see more')
  })

  it('knows whether the start is loaded', () => {
    expect(startNotLoaded({ startOffset: 10 })).toBe(true)
    expect(startNotLoaded({ startOffset: 0 })).toBe(false)
    expect(startNotLoaded({})).toBe(false)
    expect(startNotLoaded(null)).toBe(false)
  })

  it('moves bookmarks down by the lines added above', () => {
    expect(shiftBookmarksDown([1, 5], 3)).toEqual([4, 8])
    expect(shiftBookmarksDown([1, 5], 0)).toEqual([1, 5])
  })

  it('starts a window after its first, cut line', () => {
    const bytes = new TextEncoder().encode('cut line\nwhole one\nwhole two\n')
    const cut = cutToFirstLine(bytes, 100)
    expect(new TextDecoder().decode(cut.bytes)).toBe('whole one\nwhole two\n')
    expect(cut.start).toBe(109)
  })

  it('keeps a window from the start of the file whole', () => {
    const bytes = new TextEncoder().encode('first\nsecond\n')
    expect(cutToFirstLine(bytes, 0)).toEqual({ bytes, start: 0 })
  })

  it('keeps a window with no line end whole, starting where it was read', () => {
    const bytes = new TextEncoder().encode('part of a very long line')
    expect(cutToFirstLine(bytes, 4096)).toEqual({ bytes, start: 4096 })
  })

  it('keeps a window whose only line end is its last byte whole, starting where it was read', () => {
    const bytes = new TextEncoder().encode('end of a very long line\n')
    expect(cutToFirstLine(bytes, 4096)).toEqual({ bytes, start: 4096 })
  })

  it('cuts in bytes, not characters', () => {
    const bytes = new TextEncoder().encode('é cut\nnext\n')
    expect(cutToFirstLine(bytes, 10).start).toBe(10 + 7)
  })
})
