import { describe, expect, it } from 'vitest'
import { formatLinesWithNumbers } from './copy-lines'

describe('formatLinesWithNumbers', () => {
  it('prefixes each line with its line number', () => {
    expect(
      formatLinesWithNumbers([
        { number: 12, text: 'first' },
        { number: 13, text: 'second' },
      ]),
    ).toBe('12: first\n13: second')
  })

  it('handles a single line', () => {
    expect(formatLinesWithNumbers([{ number: 1, text: 'only' }])).toBe('1: only')
  })

  it('returns an empty string for no lines', () => {
    expect(formatLinesWithNumbers([])).toBe('')
  })

  it('preserves an empty line body', () => {
    expect(formatLinesWithNumbers([{ number: 5, text: '' }])).toBe('5: ')
  })
})
