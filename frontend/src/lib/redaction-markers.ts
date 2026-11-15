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
// or one too long to read whole.
const UNCOUNTED = /^\[redacted: (incomplete line|line over [^\]]*)\]$/

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

export function redactionCountLabel(count: number): string {
  return count === 1 ? '1 value redacted' : `${count.toLocaleString('en')} values redacted`
}
