export interface NumberedLine {
  number: number
  text: string
}

/** Formats a run of lines as "N: text", one per line, joined by \n --
 * the actual clipboard payload for "copy selected lines with line
 * numbers" (ROADMAP.md's "Viewer: toward an advanced editor" toolbar
 * items). Pure so it's testable without a CodeMirror view; the caller
 * (CodeMirrorPane) is responsible for turning a selection into this
 * shape and writing the result to the clipboard. */
export function formatLinesWithNumbers(lines: NumberedLine[]): string {
  return lines.map((line) => `${line.number}: ${line.text}`).join('\n')
}
