import { afterEach, describe, expect, it, vi } from 'vitest'
import { HIGHLIGHT_MAX_CHARS, matchLines, PATTERN_BUDGET_MS } from './severity-matching'
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

function inLine(line: string, patterns: SeverityPattern[]) {
  return matchLines([line], patterns).matches[0]
}

describe('matchLines', () => {
  it('matches a literal (glob-kind) pattern case-insensitively, every occurrence', () => {
    const matches = inLine('ERROR: retry after error', [pattern({ pattern: 'error' })])
    expect(matches).toHaveLength(2)
    expect(matches[0]).toMatchObject({ matchStart: 0, matchLength: 5 })
    expect(matches[1]).toMatchObject({ matchStart: 19, matchLength: 5 })
  })

  it('treats glob-kind wildcards as literal text, not path globbing', () => {
    const matches = inLine('a*b literal', [pattern({ pattern: 'a*b' })])
    expect(matches).toHaveLength(1)
    expect(matches[0].matchStart).toBe(0)
  })

  it('matches a regex-kind pattern', () => {
    const matches = inLine('retry #42 failed', [pattern({ pattern: '\\d+', pattern_kind: 'regex' })])
    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({ matchStart: 7, matchLength: 2 })
  })

  it('skips an invalid regex pattern instead of throwing', () => {
    const invalid = pattern({ pattern: '(unclosed', pattern_kind: 'regex' })
    expect(() => inLine('anything', [invalid])).not.toThrow()
    expect(inLine('anything', [invalid])).toEqual([])
  })

  it('does not infinite-loop on a zero-length regex match', () => {
    expect(inLine('abc', [pattern({ pattern: 'x*', pattern_kind: 'regex' })])).toEqual([])
  })

  it('skips disabled patterns', () => {
    expect(inLine('error here', [pattern({ enabled: false })])).toEqual([])
  })

  it('combines matches from multiple independent patterns', () => {
    const matches = inLine('warn: retry, then error', [
      pattern({ pattern: 'warn', level: 'warning' }),
      pattern({ pattern: 'error', level: 'error' }),
    ])
    expect(matches.map((m) => m.level)).toEqual(['warning', 'error'])
  })

  it('gives the matches of each line, and says before each pattern runs', () => {
    const started: number[] = []
    const { matches, slow } = matchLines(
      ['ok', 'error', 'warn and error'],
      [pattern({ id: 1, pattern: 'error' }), pattern({ id: 2, pattern: 'warn' })],
      (id) => started.push(id),
    )
    expect(matches.map((m) => m.length)).toEqual([0, 1, 2])
    expect(started).toEqual([1, 2])
    expect(slow).toEqual([])
  })
})

describe('bounds on what a pattern costs the browser', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('matches only the first 2000 characters of a line', () => {
    expect(HIGHLIGHT_MAX_CHARS).toBe(2000)
    const p = pattern({ pattern: 'error' })
    expect(inLine('x'.repeat(1995) + 'error', [p])).toHaveLength(1)
    expect(inLine('x'.repeat(1996) + 'error', [p])).toEqual([])
    expect(inLine('x'.repeat(5000) + 'error', [p])).toEqual([])
  })

  it('counts a pattern’s time over the whole pass, not line by line', () => {
    let now = 0
    // Every clock reading is 1% of the budget later: a line costs the
    // pattern that matches it two readings, the one that doesn't one. Each
    // line is cheap; 75 lines together are over the budget for the first.
    vi.spyOn(performance, 'now').mockImplementation(() => (now += PATTERN_BUDGET_MS / 100))
    const lines = Array.from({ length: 75 }, () => 'error')
    const slow = pattern({ id: 7, pattern: 'error' })
    const fine = pattern({ id: 8, pattern: 'zzz' })

    const result = matchLines(lines, [slow, fine])
    expect(result.slow).toEqual([7])
    // None of a slow pattern's matches are kept.
    expect(result.matches.every((m) => m.length === 0)).toBe(true)
  })

  it('keeps a pattern within its budget', () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => (now += 1))
    const result = matchLines(['error', 'error'], [pattern({ id: 9 })])
    expect(result.slow).toEqual([])
    expect(result.matches.map((m) => m.length)).toEqual([1, 1])
  })
})
