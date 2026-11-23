import { describe, expect, it } from 'vitest'
import {
  countRedactionMarkers,
  findRedactionMarkers,
  redactionCountAfterCap,
  redactionCountLabel,
} from './redaction-markers'
import { capTailBuffer } from './viewer-limits'

describe('countRedactionMarkers', () => {
  it('counts the redacted values across lines', () => {
    expect(countRedactionMarkers('a=[redacted: email]\nb [redacted: card ****1111] [redacted: ip]\n')).toBe(3)
    expect(countRedactionMarkers('nothing here\n')).toBe(0)
  })

  it("leaves out the markers the server doesn't count", () => {
    expect(countRedactionMarkers('[redacted: incomplete line]')).toBe(0)
    expect(countRedactionMarkers('x[redacted: line over 1008 KiB]\n[redacted: jwt]\n')).toBe(1)
    // The rest of a file that turned UTF-32 part way (X-Redaction-Count
    // doesn't count it either).
    expect(countRedactionMarkers('ok [redacted: email]\n[redacted: unsupported encoding]')).toBe(1)
    // Still highlighted as a marker.
    expect(findRedactionMarkers('[redacted: unsupported encoding]')).toEqual([{ start: 0, end: 32 }])
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

describe('redactionCountAfterCap', () => {
  it('counts only the values left in the Tail buffer once lines leave its top', () => {
    const held =
      'a [redacted: email] [redacted: ip]\n' +
      'b [redacted: jwt]\n' +
      'c plain\n' +
      'd [redacted: card ****1111]\n' +
      '[redacted: incomplete line]'
    // Five lines, four newlines; at most two kept: the first two go.
    const capped = capTailBuffer(held, 4, 2)
    expect(capped.dropped).toBe(2)
    const count = redactionCountAfterCap(held, capped.content, 4)
    expect(count).toBe(1)
    expect(count).toBe(countRedactionMarkers(capped.content))
  })

  it('keeps the count when nothing left the top', () => {
    const held = 'a [redacted: email]\n'
    expect(redactionCountAfterCap(held, held, 1)).toBe(1)
  })

  it('never goes below zero', () => {
    expect(redactionCountAfterCap('[redacted: ip] [redacted: ip]\nx\n', 'x\n', 1)).toBe(0)
  })
})

describe('redactionCountLabel', () => {
  it('says how many values were redacted', () => {
    expect(redactionCountLabel(0)).toBe('0 values redacted')
    expect(redactionCountLabel(1)).toBe('1 value redacted')
    expect(redactionCountLabel(1234)).toBe('1,234 values redacted')
  })
})
