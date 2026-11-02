import { describe, expect, it } from 'vitest'
import { findMarkMatches, nextMarkColorIndex, type MarkPattern } from './mark-highlighting'

function mark(overrides: Partial<MarkPattern> = {}): MarkPattern {
  return {
    id: 'm1',
    pattern: 'timeout',
    pattern_kind: 'glob',
    colorIndex: 0,
    ...overrides,
  }
}

describe('findMarkMatches', () => {
  it('matches a literal (glob-kind) pattern case-insensitively, every occurrence', () => {
    const matches = findMarkMatches('Timeout waiting, retry timeout', [mark()])
    expect(matches).toHaveLength(2)
    expect(matches[0]).toMatchObject({ matchStart: 0, matchLength: 7, colorIndex: 0 })
    expect(matches[1]).toMatchObject({ matchStart: 23, matchLength: 7, colorIndex: 0 })
  })

  it('treats glob-kind wildcards as literal text', () => {
    const matches = findMarkMatches('a*b literal', [mark({ pattern: 'a*b' })])
    expect(matches).toHaveLength(1)
  })

  it('matches a regex-kind pattern', () => {
    const matches = findMarkMatches('req id=42 failed', [
      mark({ pattern: 'id=\\d+', pattern_kind: 'regex' }),
    ])
    expect(matches).toHaveLength(1)
    expect(matches[0]).toMatchObject({ matchStart: 4, matchLength: 5 })
  })

  it('applies multiple marks independently, each keeping its own color', () => {
    const matches = findMarkMatches('error then warning then error', [
      mark({ id: 'a', pattern: 'error', colorIndex: 0 }),
      mark({ id: 'b', pattern: 'warning', colorIndex: 1 }),
    ])
    expect(matches).toHaveLength(3)
    expect(matches.filter((m) => m.colorIndex === 0)).toHaveLength(2)
    expect(matches.filter((m) => m.colorIndex === 1)).toHaveLength(1)
  })

  it('ignores an invalid regex pattern without throwing or affecting other marks', () => {
    const matches = findMarkMatches('valid text here', [
      mark({ id: 'bad', pattern: '(unterminated', pattern_kind: 'regex' }),
      mark({ id: 'good', pattern: 'valid', colorIndex: 1 }),
    ])
    expect(matches).toHaveLength(1)
    expect(matches[0].colorIndex).toBe(1)
  })

  it('ignores an empty pattern', () => {
    expect(findMarkMatches('anything', [mark({ pattern: '' })])).toHaveLength(0)
  })
})

describe('nextMarkColorIndex', () => {
  it('starts at 0 with no existing marks', () => {
    expect(nextMarkColorIndex([])).toBe(0)
  })

  it('picks the next unused color', () => {
    expect(nextMarkColorIndex([mark({ colorIndex: 0 }), mark({ colorIndex: 1 })])).toBe(2)
  })

  it('fills a gap left by a removed mark before extending', () => {
    expect(nextMarkColorIndex([mark({ colorIndex: 0 }), mark({ colorIndex: 2 })])).toBe(1)
  })

  it('cycles back to 0 once every color is in use', () => {
    const all = Array.from({ length: 6 }, (_, i) => mark({ id: `m${i}`, colorIndex: i }))
    expect(nextMarkColorIndex(all)).toBe(0)
  })
})
