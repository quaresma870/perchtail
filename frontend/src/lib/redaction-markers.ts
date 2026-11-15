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

export function redactionCountLabel(count: number): string {
  return count === 1 ? '1 value redacted' : `${count.toLocaleString('en')} values redacted`
}
