import { describe, expect, it } from 'vitest'
import { findRedactionMarkers, redactionCountLabel } from './redaction-markers'

describe('findRedactionMarkers', () => {
  it('finds every marker in a line', () => {
    const line = 'user=[redacted: email] card [redacted: card ****1111] ok'
    expect(findRedactionMarkers(line).map((m) => line.slice(m.start, m.end))).toEqual([
      '[redacted: email]',
      '[redacted: card ****1111]',
    ])
  })

  it('finds nothing in a line without one', () => {
    expect(findRedactionMarkers('plain [redacted] line')).toEqual([])
    expect(findRedactionMarkers('[redacted: unclosed')).toEqual([])
  })
})

describe('redactionCountLabel', () => {
  it('says how many values were redacted', () => {
    expect(redactionCountLabel(0)).toBe('0 values redacted')
    expect(redactionCountLabel(1)).toBe('1 value redacted')
    expect(redactionCountLabel(1234)).toBe('1,234 values redacted')
  })
})
