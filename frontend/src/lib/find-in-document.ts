// Notepad++-style "Find All in Current Document": scans the already-fetched
// tab content directly (plain JS, no CodeMirror view needed) rather than
// walking view.state.doc -- the tab's content string is the same data either
// way, and this keeps the matching logic pure and unit-testable on its own.
//
// There is no limit on the results: logs reach 100+ MB with millions of
// matches (and single lines of millions of characters), so the scan keeps
// only the numbers of the matching lines. Where each match sits, for the
// highlight, is worked out only for the rows on screen (rowView).

export interface FindOptions {
  caseSensitive?: boolean
  useRegex?: boolean
  /** Only matches not inside a longer word (letters, digits or _ around). */
  wholeWord?: boolean
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** What the find bar searches for (#299): shared by the editor's
 * highlights and next/previous, the count, and Find all. */
export interface FindSpec {
  query: string
  caseSensitive: boolean
  useRegex: boolean
  wholeWord: boolean
}

/** The query as a global regex, or null when it's empty or (while typing)
 * an invalid regex -- no matches rather than an error. */
export function compileFind(query: string, options: FindOptions = {}): RegExp | null {
  if (!query) return null
  const source = options.useRegex ? query : escapeRegex(query)
  // Always unicode (u), as CodeMirror compiles a regex: the editor
  // highlights this same regex (see CodeMirrorPane), so the count and Find
  // all agree with what's highlighted.
  const flags = options.caseSensitive ? 'gu' : 'giu'
  try {
    if (options.wholeWord) {
      return new RegExp(`(?<![\\p{L}\\p{N}_])(?:${source})(?![\\p{L}\\p{N}_])`, flags)
    }
    return new RegExp(source, flags)
  } catch {
    return null
  }
}

function lineMatches(pattern: RegExp, text: string): boolean {
  pattern.lastIndex = 0
  return pattern.test(text)
}

/** The 1-based numbers of every line the pattern matches, in file order. */
export function matchingLines(lines: string[], pattern: RegExp | null): number[] {
  if (!pattern) return []
  const out: number[] = []
  for (let i = 0; i < lines.length; i++) {
    if (lineMatches(pattern, lines[i])) out.push(i + 1)
  }
  return out
}

export interface RefineStep {
  query: string
  caseSensitive: boolean
  useRegex: boolean
  /** true = drop lines that match (Notepad++'s "exclude"), false = keep only matches. */
  exclude: boolean
}

/** "Search in these results" / "Exclude lines matching…": narrows an
 * existing result set without re-scanning the whole file. The original
 * highlights stay; an invalid regex leaves the lines unchanged. */
export function refineLines(lines: string[], found: number[], step: RefineStep): number[] {
  const pattern = compileFind(step.query, step)
  if (!pattern) return found
  return found.filter((n) => lineMatches(pattern, lines[n - 1]) !== step.exclude)
}

/** Text for "Copy selected lines" (optionally with line numbers), in file
 * order regardless of the order lines were selected in. */
export function copyText(lines: string[], found: number[], selected: Set<number>, withNumbers: boolean): string {
  return found
    .filter((n) => selected.has(n))
    .map((n) => (withNumbers ? `${n}: ${lines[n - 1]}` : lines[n - 1]))
    .join('\n')
}

export interface Range {
  start: number
  length: number
}

/** Splits text into plain and highlighted segments for rendering. */
export function segments(text: string, ranges: Range[]): { text: string; hit: boolean }[] {
  const sorted = [...ranges].sort((a, b) => a.start - b.start)
  const out: { text: string; hit: boolean }[] = []
  let pos = 0
  for (const r of sorted) {
    if (r.start < pos) continue // overlapping match: already covered
    if (r.start > pos) out.push({ text: text.slice(pos, r.start), hit: false })
    if (r.length > 0) out.push({ text: text.slice(r.start, r.start + r.length), hit: true })
    pos = r.start + r.length
  }
  if (pos < text.length) out.push({ text: text.slice(pos), hit: false })
  return out
}

/** How much of one line a row draws. A browser can't lay out a line of
 * millions of characters in one element, and wide rows by the thousand are
 * what made the list lose rows while scrolling; the whole line is still
 * what's copied, and double-click opens it in the editor. */
export const MAX_ROW_CHARS = 10_000
/** Characters kept before the first match when the row starts later. */
export const ROW_CONTEXT_CHARS = 200

export interface RowView {
  segments: { text: string; hit: boolean }[]
  /** Text was left out before / after what the row shows. */
  before: boolean
  after: boolean
  /** Every (non-empty) match on the line, not only the ones drawn. */
  hits: number
}

/** What a row draws: the line with its matches highlighted, cut to at most
 * `max` characters -- from the start, or from a little before the first
 * match when it lies further on, so a row always shows a match. */
export function rowView(text: string, pattern: RegExp, max = MAX_ROW_CHARS, context = ROW_CONTEXT_CHARS): RowView {
  let hits = 0
  let firstHit = -1
  pattern.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = pattern.exec(text)) !== null) {
    if (m[0].length > 0) {
      hits++
      if (firstHit < 0) firstHit = m.index
    } else {
      pattern.lastIndex++ // step past an empty match
    }
  }

  let from = 0
  if (text.length > max && firstHit + context >= max) from = Math.max(0, firstHit - context)
  const to = Math.min(text.length, from + max)
  const window = text.slice(from, to)

  const ranges: Range[] = []
  pattern.lastIndex = from
  while ((m = pattern.exec(text)) !== null && m.index < to) {
    if (m[0].length === 0) {
      pattern.lastIndex++
      continue
    }
    ranges.push({ start: m.index - from, length: Math.min(m.index + m[0].length, to) - m.index })
  }
  return { segments: segments(window, ranges), before: from > 0, after: to < text.length, hits }
}

/** The characters a row shows, counting the "…" markers of a cut line. */
export function shownLength(text: string, max = MAX_ROW_CHARS): number {
  return text.length <= max ? text.length : max + 2
}

export interface Snippet {
  text: string
  matchStart: number
  matchLength: number
}

/** Windows a possibly very long line down to a fixed radius around the
 * match (minified JSON/XML embedded in a log line can be thousands of
 * characters), returning offsets relative to the windowed text so the
 * caller can still highlight exactly the matched portion. */
export function snippetAround(
  lineText: string,
  matchStart: number,
  matchLength: number,
  radius = 60,
): Snippet {
  const start = Math.max(0, matchStart - radius)
  const end = Math.min(lineText.length, matchStart + matchLength + radius)
  const prefix = start > 0 ? '…' : ''
  const suffix = end < lineText.length ? '…' : ''
  return {
    text: prefix + lineText.slice(start, end) + suffix,
    matchStart: matchStart - start + prefix.length,
    matchLength,
  }
}
