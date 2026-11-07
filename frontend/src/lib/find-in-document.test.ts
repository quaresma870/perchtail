import { describe, expect, it } from 'vitest'
import {
  compileFind,
  copyText,
  matchingLines,
  refineLines,
  rowView,
  segments,
  shownLength,
  snippetAround,
} from './find-in-document'

const found = (content: string, query: string, options = {}) =>
  matchingLines(content.split('\n'), compileFind(query, options))

describe('matchingLines', () => {
  const content = ['line one error here', 'line two ok', 'ERROR again on line three', 'error error same line'].join(
    '\n',
  )

  it('finds nothing for an empty query or an invalid regex', () => {
    expect(compileFind('')).toBeNull()
    expect(compileFind('(unclosed', { useRegex: true })).toBeNull()
    expect(found(content, '')).toEqual([])
    expect(found(content, '(unclosed', { useRegex: true })).toEqual([])
  })

  it('matches case-insensitively by default, one entry per line', () => {
    expect(found(content, 'error')).toEqual([1, 3, 4])
  })

  it('respects caseSensitive: true', () => {
    expect(found(content, 'error', { caseSensitive: true })).toEqual([1, 4])
  })

  it('treats the query as a literal string by default, not a regex', () => {
    expect(found('a.b\nabc', '.')).toEqual([1])
  })

  it('supports regex mode, empty matches included', () => {
    expect(found('a1\nb\nc333', '\\d+', { useRegex: true })).toEqual([1, 3])
    expect(found('abc\n\nx', '^', { useRegex: true })).toEqual([1, 2, 3])
  })

  it('finds every line, with no limit', () => {
    const many = Array.from({ length: 20000 }, () => 'error').join('\n')
    expect(found(many, 'error')).toHaveLength(20000)
  })
})

describe('rowView', () => {
  const pattern = (q: string) => compileFind(q)!

  it('highlights every match of a short line and counts them', () => {
    expect(rowView('a-error-b-error', pattern('error'))).toEqual({
      segments: [
        { text: 'a-', hit: false },
        { text: 'error', hit: true },
        { text: '-b-', hit: false },
        { text: 'error', hit: true },
      ],
      before: false,
      after: false,
      hits: 2,
    })
  })

  it('cuts a long line from the start when the match is near it', () => {
    const view = rowView(`ab-error-${'x'.repeat(50)}`, pattern('error'), 20, 5)
    expect(view.before).toBe(false)
    expect(view.after).toBe(true)
    expect(view.segments).toEqual([
      { text: 'ab-', hit: false },
      { text: 'error', hit: true },
      { text: '-xxxxxxxxxxx', hit: false },
    ])
  })

  it('starts a long line a little before a match that lies further on, counting all of them', () => {
    const view = rowView(`${'x'.repeat(40)}error${'y'.repeat(40)}error`, pattern('error'), 20, 5)
    expect(view).toEqual({
      segments: [
        { text: 'xxxxx', hit: false },
        { text: 'error', hit: true },
        { text: 'yyyyyyyyyy', hit: false },
      ],
      before: true,
      after: true,
      hits: 2,
    })
  })

  it('cuts a match that crosses the end of the window', () => {
    const view = rowView(`${'x'.repeat(18)}error${'y'.repeat(40)}`, pattern('error'), 20, 1)
    expect(view.segments.at(-1)).toEqual({ text: 'er', hit: true })
  })

  it('draws a line matched only by an empty match without highlights', () => {
    const view = rowView('abc', compileFind('x*', { useRegex: true })!)
    expect(view).toEqual({ segments: [{ text: 'abc', hit: false }], before: false, after: false, hits: 0 })
  })

  it('counts the "…" markers in the shown length', () => {
    expect(shownLength('abc', 20)).toBe(3)
    expect(shownLength('x'.repeat(50), 20)).toBe(22)
  })
})

describe('snippetAround', () => {
  it('returns the whole line unchanged when it fits within the radius', () => {
    const result = snippetAround('short line with error', 17, 5, 60)
    expect(result).toEqual({ text: 'short line with error', matchStart: 17, matchLength: 5 })
  })

  it('windows a long line and adjusts matchStart to the windowed text', () => {
    const long = 'x'.repeat(200) + 'ERROR' + 'y'.repeat(200)
    const result = snippetAround(long, 200, 5, 10)
    expect(result.text).toBe('…' + 'x'.repeat(10) + 'ERROR' + 'y'.repeat(10) + '…')
    expect(result.text.slice(result.matchStart, result.matchStart + result.matchLength)).toBe('ERROR')
  })

  it('omits the leading ellipsis when the window starts at the beginning of the line', () => {
    const result = snippetAround('ERROR' + 'y'.repeat(200), 0, 5, 10)
    expect(result.text.startsWith('…')).toBe(false)
    expect(result.text.slice(result.matchStart, result.matchStart + result.matchLength)).toBe('ERROR')
  })
})

describe('Find All result lines (#155)', () => {
  const lines = 'error one error\nok\nerror two\nwarn three'.split('\n')

  it('refines results by search and by exclusion, chained', () => {
    const all = matchingLines(lines, compileFind('e'))
    const kept = refineLines(lines, all, { query: 'error', caseSensitive: false, useRegex: false, exclude: false })
    expect(kept).toEqual([1, 3])
    const dropped = refineLines(lines, kept, { query: 'two', caseSensitive: false, useRegex: false, exclude: true })
    expect(dropped).toEqual([1])
  })

  it('leaves results unchanged for an invalid regex', () => {
    const all = matchingLines(lines, compileFind('e'))
    expect(refineLines(lines, all, { query: '(', caseSensitive: false, useRegex: true, exclude: false })).toEqual(all)
  })

  it('copies selected lines in file order, with or without numbers', () => {
    const all = matchingLines(lines, compileFind('e'))
    const selected = new Set([3, 1])
    expect(copyText(lines, all, selected, false)).toBe('error one error\nerror two')
    expect(copyText(lines, all, selected, true)).toBe('1: error one error\n3: error two')
  })

  it('splits text into highlighted segments', () => {
    expect(segments('a-error-b', [{ start: 2, length: 5 }])).toEqual([
      { text: 'a-', hit: false },
      { text: 'error', hit: true },
      { text: '-b', hit: false },
    ])
  })
})
