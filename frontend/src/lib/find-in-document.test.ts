import { describe, expect, it } from 'vitest'
import { findAllMatches, snippetAround } from './find-in-document'

describe('findAllMatches', () => {
  const content = ['line one error here', 'line two ok', 'ERROR again on line three', 'error error same line'].join(
    '\n',
  )

  it('returns no matches for an empty query', () => {
    expect(findAllMatches(content, '')).toEqual({ matches: [], truncated: false })
  })

  it('matches case-insensitively by default', () => {
    const result = findAllMatches(content, 'error')
    expect(result.matches.map((m) => m.line)).toEqual([1, 3, 4, 4])
    expect(result.truncated).toBe(false)
  })

  it('respects caseSensitive: true', () => {
    const result = findAllMatches(content, 'error', { caseSensitive: true })
    expect(result.matches.map((m) => m.line)).toEqual([1, 4, 4])
  })

  it('records the column offset and length of each match', () => {
    const result = findAllMatches('foo bar foo', 'foo')
    expect(result.matches).toEqual([
      { line: 1, lineText: 'foo bar foo', matchStart: 0, matchLength: 3 },
      { line: 1, lineText: 'foo bar foo', matchStart: 8, matchLength: 3 },
    ])
  })

  it('treats the query as a literal string by default, not a regex', () => {
    const result = findAllMatches('a.b.c', '.')
    // literal "." should only match the two literal dots, not every char
    expect(result.matches).toHaveLength(2)
  })

  it('supports regex mode', () => {
    const result = findAllMatches('a1 b22 c333', '\\d+', { useRegex: true })
    expect(result.matches.map((m) => m.lineText.slice(m.matchStart, m.matchStart + m.matchLength))).toEqual([
      '1',
      '22',
      '333',
    ])
  })

  it('returns no matches for an invalid regex rather than throwing', () => {
    expect(() => findAllMatches('anything', '(unclosed', { useRegex: true })).not.toThrow()
    expect(findAllMatches('anything', '(unclosed', { useRegex: true })).toEqual({
      matches: [],
      truncated: false,
    })
  })

  it('does not loop forever on a zero-length-match regex', () => {
    const result = findAllMatches('abc', 'x*', { useRegex: true, maxResults: 100 })
    expect(result.truncated).toBe(false)
    expect(result.matches.length).toBeGreaterThan(0)
  })

  it('truncates at maxResults and reports truncated: true', () => {
    const manyLines = Array.from({ length: 10 }, () => 'error').join('\n')
    const result = findAllMatches(manyLines, 'error', { maxResults: 3 })
    expect(result.matches).toHaveLength(3)
    expect(result.truncated).toBe(true)
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
