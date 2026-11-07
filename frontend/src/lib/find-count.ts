// The find bar's "3 of 120" (#299). Every match is counted, with no limit,
// but only per line (one number per line, not one per match: a 100 MB log
// can hold tens of millions of matches), and in short slices so a big file
// doesn't freeze the page while it's counted.

export interface MatchCount {
  /** Matches on each line (index 0 = line 1). */
  perLine: Int32Array
  total: number
  done: boolean
}

/** Non-empty matches of `pattern` (global) in one line. */
export function countInLine(pattern: RegExp, text: string, before = Infinity): number {
  let n = 0
  pattern.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = pattern.exec(text)) !== null && m.index < before) {
    if (m[0].length === 0) pattern.lastIndex++
    else n++
  }
  return n
}

/** Counts in slices of about `sliceMs`, calling `onProgress` after each one
 * (and once more when done). Returns a function that stops it. */
export function countMatches(
  lines: string[],
  pattern: RegExp,
  onProgress: (count: MatchCount) => void,
  sliceMs = 12,
): () => void {
  const count: MatchCount = { perLine: new Int32Array(lines.length), total: 0, done: false }
  let next = 0
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | null = null

  function slice() {
    const until = performance.now() + sliceMs
    while (next < lines.length && performance.now() < until) {
      // A few lines between clock reads: performance.now() isn't free.
      const end = Math.min(lines.length, next + 64)
      for (; next < end; next++) {
        const n = countInLine(pattern, lines[next])
        count.perLine[next] = n
        count.total += n
      }
    }
    count.done = next >= lines.length
    if (stopped) return
    onProgress(count)
    if (!count.done) timer = setTimeout(slice, 0)
  }
  slice()
  return () => {
    stopped = true
    if (timer) clearTimeout(timer)
  }
}

/** The 1-based position of the match starting at (line, column) among all
 * of them -- or, when no match starts there, of the next one; 0 when there
 * is none at or after it. */
export function matchIndexAt(
  count: MatchCount,
  lines: string[],
  pattern: RegExp,
  line: number,
  column: number,
): number {
  if (count.total === 0) return 0
  let before = 0
  for (let i = 0; i < line - 1 && i < count.perLine.length; i++) before += count.perLine[i]
  before += countInLine(pattern, lines[line - 1] ?? '', column)
  return before < count.total ? before + 1 : 0
}

/** After text was added at the end (Tail): recounts only from the old last
 * line, which may have grown, on. `tail` is the new text from the start of
 * that line. */
export function countAppended(
  count: MatchCount,
  lines: string[],
  tail: string,
  pattern: RegExp,
): { lines: string[]; count: MatchCount } {
  const keep = Math.max(0, lines.length - 1)
  const next = lines.slice(0, keep).concat(tail.split('\n'))
  const perLine = new Int32Array(next.length)
  perLine.set(count.perLine.subarray(0, keep))
  let total = count.total - (lines.length > 0 ? count.perLine[keep] : 0)
  for (let i = keep; i < next.length; i++) {
    const n = countInLine(pattern, next[i])
    perLine[i] = n
    total += n
  }
  return { lines: next, count: { perLine, total, done: true } }
}
