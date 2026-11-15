import { describe, expect, it } from 'vitest'
import { countRedactionMarkers, findRedactionMarkers, redactionCountLabel } from './redaction-markers'

describe('countRedactionMarkers', () => {
  it('counts the redacted values across lines', () => {
    expect(countRedactionMarkers('a=[redacted: email]\nb [redacted: card ****1111] [redacted: ip]\n')).toBe(3)
    expect(countRedactionMarkers('nothing here\n')).toBe(0)
  })

  it("leaves out the markers the server doesn't count", () => {
    expect(countRedactionMarkers('[redacted: incomplete line]')).toBe(0)
    expect(countRedactionMarkers('x[redacted: line over 1008 KiB]\n[redacted: jwt]\n')).toBe(1)
  })
})

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
