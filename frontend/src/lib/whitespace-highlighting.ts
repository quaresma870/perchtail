export interface WhitespaceRun {
  start: number
  length: number
  char: ' ' | '\t'
}

/** Scans one line for runs of spaces/tabs -- consecutive characters of the
 * *same* kind are grouped into a single run (a run never mixes spaces and
 * tabs), so the caller can render one glyph-widget per run instead of one
 * per character. Pure so it's testable without a CodeMirror view; the
 * actual glyph rendering (middle dots for spaces, arrows for tabs) lives
 * in codemirror-theme.ts's ViewPlugin glue. */
export function findWhitespaceRuns(lineText: string): WhitespaceRun[] {
  const runs: WhitespaceRun[] = []
  let i = 0
  while (i < lineText.length) {
    const ch = lineText[i]
    if (ch === ' ' || ch === '\t') {
      let j = i + 1
      while (j < lineText.length && lineText[j] === ch) j += 1
      runs.push({ start: i, length: j - i, char: ch })
      i = j
    } else {
      i += 1
    }
  }
  return runs
}

/** CodeMirror's own line separator matching (`/\r\n?|\n/`, its default)
 * treats a `\r\n` pair as a single line break and consumes the `\r` as
 * part of that separator -- by the time a `Line`'s `.text` can be
 * inspected from a live `EditorView`, the `\r` is already gone, so CRLF
 * can't be detected there. This instead scans the raw fetched content
 * *before* CodeMirror ever ingests it, splitting on `\n` the same way
 * (a `\r\n` pair collapses to one separator either way, so line numbers
 * line up with CodeMirror's own for the LF/CRLF files this tool actually
 * sees). Returns the set of 1-indexed line numbers that end in `\r` --
 * how the "show all characters" toggle flags CRLF lines, relevant given
 * this tool spans both Linux and Windows sources (CLAUDE.md). */
export function findCrlfLineNumbers(content: string): Set<number> {
  const result = new Set<number>()
  content.split('\n').forEach((raw, index) => {
    if (raw.endsWith('\r')) result.add(index + 1)
  })
  return result
}
