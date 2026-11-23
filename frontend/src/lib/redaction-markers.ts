// Redaction (#382, #249): the server replaces secrets and personal data
// with `[redacted: <label>]` before content reaches the browser. The viewer
// only shows them: it highlights the markers and says how many there are.

const MARKER = /\[redacted: [^\]\n]{1,64}\]/g

export interface MarkerMatch {
  start: number
  end: number
}

/** The redaction markers in one line of text. A log that already holds
 * this text is highlighted too, which does no harm. */
export function findRedactionMarkers(line: string): MarkerMatch[] {
  const out: MarkerMatch[] = []
  if (!line.includes('[redacted: ')) return out
  for (const match of line.matchAll(MARKER)) {
    out.push({ start: match.index, end: match.index + match[0].length })
  }
  return out
}

// Markers the server doesn't count as a value: a line still being written,
// one too long to read whole, or the rest of a file in an encoding
// redaction doesn't support.
const UNCOUNTED = /^\[redacted: (incomplete line|line over [^\]]*|unsupported encoding)\]$/

/** How many redacted values `text` shows, the way the server counts them
 * in X-Redaction-Count: used to take the lines the Tail drops from the top
 * off the count. */
export function countRedactionMarkers(text: string): number {
  if (!text.includes('[redacted: ')) return 0
  let count = 0
  for (const match of text.matchAll(MARKER)) {
    if (!UNCOUNTED.test(match[0])) count++
  }
  return count
}

/** The redacted-value count of a Tail buffer once lines left its top
 * (capTailBuffer): `held` is the buffer before the cut, `kept` what stays
 * (its end), `count` the count of `held`. The count is of what the tab
 * shows, so the values in the lines that went are taken off. */
export function redactionCountAfterCap(held: string, kept: string, count: number): number {
  if (count <= 0 || kept.length >= held.length) return count
  const gone = held.slice(0, held.length - kept.length)
  return Math.max(0, count - countRedactionMarkers(gone))
}

export function redactionCountLabel(count: number): string {
  return count === 1 ? '1 value redacted' : `${count.toLocaleString('en')} values redacted`
}
