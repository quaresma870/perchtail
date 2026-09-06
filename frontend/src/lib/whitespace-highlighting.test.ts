import { describe, expect, it } from 'vitest'
import { findCrlfLineNumbers, findWhitespaceRuns } from './whitespace-highlighting'

describe('findWhitespaceRuns', () => {
  it('finds a single run of spaces', () => {
    expect(findWhitespaceRuns('a   b')).toEqual([{ start: 1, length: 3, char: ' ' }])
  })

  it('finds a single run of tabs', () => {
    expect(findWhitespaceRuns('a\t\tb')).toEqual([{ start: 1, length: 2, char: '\t' }])
  })

  it('splits a mixed space/tab run into separate homogeneous runs', () => {
    expect(findWhitespaceRuns('a \t b')).toEqual([
      { start: 1, length: 1, char: ' ' },
      { start: 2, length: 1, char: '\t' },
      { start: 3, length: 1, char: ' ' },
    ])
  })

  it('finds multiple separate runs', () => {
    expect(findWhitespaceRuns('a  b  c')).toEqual([
      { start: 1, length: 2, char: ' ' },
      { start: 4, length: 2, char: ' ' },
    ])
  })

  it('finds leading and trailing whitespace', () => {
    expect(findWhitespaceRuns('  a  ')).toEqual([
      { start: 0, length: 2, char: ' ' },
      { start: 3, length: 2, char: ' ' },
    ])
  })

  it('returns nothing for a line with no whitespace', () => {
    expect(findWhitespaceRuns('abc')).toEqual([])
  })

  it('returns nothing for an empty line', () => {
    expect(findWhitespaceRuns('')).toEqual([])
  })
})

describe('findCrlfLineNumbers', () => {
  it('flags lines ending in \\r\\n, leaves plain LF lines out', () => {
    const content = 'one\r\ntwo\nthree\r\n'
    expect(findCrlfLineNumbers(content)).toEqual(new Set([1, 3]))
  })

  it('returns an empty set for an all-LF file', () => {
    expect(findCrlfLineNumbers('one\ntwo\nthree')).toEqual(new Set())
  })

  it('returns an empty set for an empty file', () => {
    expect(findCrlfLineNumbers('')).toEqual(new Set())
  })

  it('flags every line when the whole file is CRLF', () => {
    expect(findCrlfLineNumbers('a\r\nb\r\nc\r\n')).toEqual(new Set([1, 2, 3]))
  })
})
