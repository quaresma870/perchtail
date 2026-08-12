import { describe, expect, it } from 'vitest'
import { nextLine, previousLine } from './line-cycle'

describe('nextLine', () => {
  it('steps to the next line after the current position', () => {
    expect(nextLine([2, 4, 9], 2)).toBe(4)
  })

  it('wraps around to the first line past the end', () => {
    expect(nextLine([2, 4, 9], 9)).toBe(2)
    expect(nextLine([2, 4, 9], 100)).toBe(2)
  })

  it('returns null when there are no lines', () => {
    expect(nextLine([], 5)).toBeNull()
  })
})

describe('previousLine', () => {
  it('steps to the previous line before the current position', () => {
    expect(previousLine([2, 4, 9], 9)).toBe(4)
  })

  it('wraps around to the last line before the start', () => {
    expect(previousLine([2, 4, 9], 2)).toBe(9)
    expect(previousLine([2, 4, 9], 1)).toBe(9)
  })

  it('returns null when there are no lines', () => {
    expect(previousLine([], 5)).toBeNull()
  })
})
