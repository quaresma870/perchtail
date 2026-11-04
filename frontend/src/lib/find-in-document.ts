// Notepad++-style "Find All in Current Document": scans the already-fetched
// tab content directly (plain JS, no CodeMirror view needed) rather than
// walking view.state.doc -- the tab's content string is the same data either
// way, and this keeps the matching logic pure and unit-testable on its own.

export interface FindMatch {
  line: number // 1-based
  lineText: string
  matchStart: number // 0-based column offset within lineText
  matchLength: number
}

export interface FindOptions {
  caseSensitive?: boolean
  useRegex?: boolean
  maxResults?: number
}

export interface FindResult {
  matches: FindMatch[]
  truncated: boolean
}

const DEFAULT_MAX_RESULTS = 5000

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function findAllMatches(content: string, query: string, options: FindOptions = {}): FindResult {
  if (!query) return { matches: [], truncated: false }

  const flags = options.caseSensitive ? 'g' : 'gi'
  let pattern: RegExp
  try {
    pattern = new RegExp(options.useRegex ? query : escapeRegex(query), flags)
  } catch {
    // Invalid regex (e.g. an unbalanced paren while still typing) -- no
    // matches rather than throwing and breaking the panel.
    return { matches: [], truncated: false }
  }

  const maxResults = options.maxResults ?? DEFAULT_MAX_RESULTS
  const matches: FindMatch[] = []
  const lines = content.split('\n')

  for (let i = 0; i < lines.length; i++) {
    const lineText = lines[i]
    pattern.lastIndex = 0
    let match: RegExpExecArray | null
    while ((match = pattern.exec(lineText)) !== null) {
      matches.push({
        line: i + 1,
        lineText,
        matchStart: match.index,
        matchLength: match[0].length,
      })
      if (matches.length >= maxResults) {
        return { matches, truncated: true }
      }
      // Zero-length matches (e.g. a regex like `x*`) would otherwise loop
      // forever at the same index.
      if (match[0].length === 0) pattern.lastIndex++
    }
  }

  return { matches, truncated: false }
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

/** One result line in the Find All panel: the whole line, with every
 * match on it (#155) -- one row per line, not per match. */
export interface LineResult {
  line: number // 1-based
  text: string
  ranges: { start: number; length: number }[]
}

export function groupMatchesByLine(matches: FindMatch[]): LineResult[] {
  const byLine = new Map<number, LineResult>()
  for (const m of matches) {
    let row = byLine.get(m.line)
    if (!row) {
      row = { line: m.line, text: m.lineText, ranges: [] }
      byLine.set(m.line, row)
    }
    row.ranges.push({ start: m.matchStart, length: m.matchLength })
  }
  return [...byLine.values()]
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
export function refineLines(lines: LineResult[], step: RefineStep): LineResult[] {
  if (!step.query) return lines
  const flags = step.caseSensitive ? '' : 'i'
  let pattern: RegExp
  try {
    pattern = new RegExp(step.useRegex ? step.query : escapeRegex(step.query), flags)
  } catch {
    return lines
  }
  return lines.filter((l) => pattern.test(l.text) !== step.exclude)
}

/** Text for "Copy selected lines" (optionally with line numbers), in file
 * order regardless of the order lines were selected in. */
export function copyText(lines: LineResult[], selected: Set<number>, withNumbers: boolean): string {
  return lines
    .filter((l) => selected.has(l.line))
    .map((l) => (withNumbers ? `${l.line}: ${l.text}` : l.text))
    .join('\n')
}

/** Splits a line into plain and highlighted segments for rendering. */
export function segments(row: LineResult): { text: string; hit: boolean }[] {
  const ranges = [...row.ranges].sort((a, b) => a.start - b.start)
  const out: { text: string; hit: boolean }[] = []
  let pos = 0
  for (const r of ranges) {
    if (r.start < pos) continue // overlapping match: already covered
    if (r.start > pos) out.push({ text: row.text.slice(pos, r.start), hit: false })
    if (r.length > 0) out.push({ text: row.text.slice(r.start, r.start + r.length), hit: true })
    pos = r.start + r.length
  }
  if (pos < row.text.length) out.push({ text: row.text.slice(pos), hit: false })
  return out
}
