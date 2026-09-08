import { describe, expect, it } from 'vitest'
import {
  findMatchesInLine,
  findProblemLines,
  lineHasHighlight,
  nextProblemLine,
  previousProblemLine,
} from './severity-highlighting'
import type { SeverityPattern } from './types'

function pattern(overrides: Partial<SeverityPattern> = {}): SeverityPattern {
  return {
    id: 1,
    source_id: null,
    level: 'error',
    pattern: 'error',
    pattern_kind: 'glob',
    enabled: true,
    highlight_line: false,
    include_in_navigation: true,
    ...overrides,
  }
}

describe('findMatchesInLine', () => {
  it('matches a literal (glob-kind) pattern case-insensitively, every occurrence', () => {
    const matches = findMatchesInLine('ERROR: retry after error', [pattern({ pattern: 'error' })])
    expect(matches).toHaveLength(2)
    expect(matches[0]).toMatchObject({ matchStart: 0, matchLength: 5 })
    expect(matches[1]).toMatchObject({ matchStart: 19, matchLength: 5 })
  })

  it('treats glob-kind wildcards as literal text, not path globbing', () => {
    const matches = findMatchesInLine('a*b literal', [pattern({ pattern: 'a*b' })])
    expect(matches).toHaveLength(1)
    expect(matches[0].matchStart).toBe(0)
  })

  it('matches a regex-kind pattern', () => {
    const matches = findMatchesInLine('retry #42 failed', [
      pattern({ pattern: '\\d+', pattern_kind: 'regex' }),
    ])
    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({ matchStart: 7, matchLength: 2 })
  })

  it('skips an invalid regex pattern instead of throwing', () => {
    expect(() =>
      findMatchesInLine('anything', [pattern({ pattern: '(unclosed', pattern_kind: 'regex' })]),
    ).not.toThrow()
    expect(findMatchesInLine('anything', [pattern({ pattern: '(unclosed', pattern_kind: 'regex' })])).toEqual(
      [],
    )
  })

  it('does not infinite-loop on a zero-length regex match', () => {
    const matches = findMatchesInLine('abc', [pattern({ pattern: 'x*', pattern_kind: 'regex' })])
    expect(matches).toEqual([])
  })

  it('skips disabled patterns', () => {
    expect(findMatchesInLine('error here', [pattern({ enabled: false })])).toEqual([])
  })

  it('combines matches from multiple independent patterns', () => {
    const matches = findMatchesInLine('warn: retry, then error', [
      pattern({ pattern: 'warn', level: 'warning' }),
      pattern({ pattern: 'error', level: 'error' }),
    ])
    expect(matches.map((m) => m.level)).toEqual(['warning', 'error'])
  })
})

describe('lineHasHighlight', () => {
  it('is true only when a matching pattern has highlight_line set', () => {
    expect(lineHasHighlight('fatal crash', [pattern({ pattern: 'fatal', highlight_line: true })])).toBe(
      true,
    )
    expect(lineHasHighlight('info: ok', [pattern({ pattern: 'info', highlight_line: false })])).toBe(
      false,
    )
  })

  it('is false when nothing matches', () => {
    expect(lineHasHighlight('all clear', [pattern({ pattern: 'error', highlight_line: true })])).toBe(
      false,
    )
  })
})

describe('findProblemLines', () => {
  const content = 'line one\nERROR here\nline three\nwarn: retry\nline five'

  it('returns 1-indexed lines with a navigation-eligible match', () => {
    expect(findProblemLines(content, [pattern({ pattern: 'error' })])).toEqual([2])
  })

  it('excludes patterns with include_in_navigation false', () => {
    expect(
      findProblemLines(content, [pattern({ pattern: 'error', include_in_navigation: false })]),
    ).toEqual([])
  })

  it('collects matches across multiple patterns, sorted ascending', () => {
    expect(
      findProblemLines(content, [
        pattern({ pattern: 'warn', level: 'warning' }),
        pattern({ pattern: 'error', level: 'error' }),
      ]),
    ).toEqual([2, 4])
  })

  it('returns empty for no patterns', () => {
    expect(findProblemLines(content, [])).toEqual([])
  })
})

describe('nextProblemLine', () => {
  it('steps to the next line after the current position', () => {
    expect(nextProblemLine([2, 4, 9], 2)).toBe(4)
  })

  it('wraps around to the first problem line past the end', () => {
    expect(nextProblemLine([2, 4, 9], 9)).toBe(2)
    expect(nextProblemLine([2, 4, 9], 100)).toBe(2)
  })

  it('returns null when there are no problem lines', () => {
    expect(nextProblemLine([], 5)).toBeNull()
  })
})

describe('previousProblemLine', () => {
  it('steps to the previous line before the current position', () => {
    expect(previousProblemLine([2, 4, 9], 9)).toBe(4)
  })

  it('wraps around to the last problem line before the start', () => {
    expect(previousProblemLine([2, 4, 9], 2)).toBe(9)
    expect(previousProblemLine([2, 4, 9], 1)).toBe(9)
  })

  it('returns null when there are no problem lines', () => {
    expect(previousProblemLine([], 5)).toBeNull()
  })
})
