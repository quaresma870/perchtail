import { describe, expect, it } from 'vitest'
import { countAppended, countInLine, countMatches, matchIndexAt, type MatchCount } from './find-count'
import { compileFind } from './find-in-document'

function countAll(lines: string[], pattern: RegExp, sliceMs = 12): Promise<MatchCount> {
  return new Promise((resolve) => {
    countMatches(lines, pattern, (c) => c.done && resolve(c), sliceMs)
  })
}

describe('countInLine', () => {
  it('counts non-empty matches, optionally only before a column', () => {
    const p = compileFind('ab')!
    expect(countInLine(p, 'ab xab AB')).toBe(3)
    expect(countInLine(p, 'ab xab AB', 5)).toBe(2)
    expect(countInLine(compileFind('x*', { useRegex: true })!, 'abc')).toBe(0)
  })

  it('honours whole word', () => {
    expect(countInLine(compileFind('log', { wholeWord: true })!, 'log logs catalog log_x log.')).toBe(2)
  })
})

describe('countMatches', () => {
  it('counts every match per line, with no limit', async () => {
    const lines = ['error error', 'ok', 'ERROR', ...Array.from({ length: 20000 }, () => 'error')]
    const count = await countAll(lines, compileFind('error')!)
    expect(count.done).toBe(true)
    expect(count.total).toBe(20003)
    expect(Array.from(count.perLine.slice(0, 3))).toEqual([2, 0, 1])
  })

  it('reports progress before it is done on a big input, and can be stopped', async () => {
    const lines = Array.from({ length: 200000 }, () => 'a b c d e f g h')
    const seen: boolean[] = []
    await new Promise<void>((resolve) => {
      const stop = countMatches(
        lines,
        compileFind(' ')!,
        (c) => {
          seen.push(c.done)
          if (seen.length === 2) {
            stop()
            resolve()
          }
        },
        1,
      )
    })
    expect(seen[0]).toBe(false)
  })
})

describe('matchIndexAt', () => {
  const lines = ['error one error', 'ok', 'error two']
  const pattern = compileFind('error')!

  it('gives the position of the match at the cursor', async () => {
    const count = await countAll(lines, pattern)
    expect(matchIndexAt(count, lines, pattern, 1, 0)).toBe(1)
    expect(matchIndexAt(count, lines, pattern, 1, 10)).toBe(2)
    expect(matchIndexAt(count, lines, pattern, 3, 0)).toBe(3)
  })

  it('gives the next match when none starts at the cursor, 0 past the last', async () => {
    const count = await countAll(lines, pattern)
    expect(matchIndexAt(count, lines, pattern, 2, 0)).toBe(3)
    expect(matchIndexAt(count, lines, pattern, 3, 5)).toBe(0)
  })
})

describe('countAppended', () => {
  it('counts only the grown last line and the new lines', async () => {
    const pattern = compileFind('error')!
    const lines = ['error', 'ok err']
    const count = await countAll(lines, pattern)
    const grown = countAppended(count, lines, 'ok error\nerror error\n', pattern)
    expect(grown.lines).toEqual(['error', 'ok error', 'error error', ''])
    expect(Array.from(grown.count.perLine)).toEqual([1, 1, 2, 0])
    expect(grown.count.total).toBe(4)
    expect(grown.count.done).toBe(true)
  })
})
