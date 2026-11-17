import type { SeverityPattern } from './types'

/** Severity patterns run in every viewer's browser, whose regex engine
 * backtracks, so they are bounded (the server also takes only RE2 syntax, at
 * most 1000 characters): only the first HIGHLIGHT_MAX_CHARS of a line are
 * matched, and they run in a Web Worker (severity-worker.ts), off the page's
 * own thread. A pattern that takes more than PATTERN_BUDGET_MS in all over
 * one pass (the lines in view, or the whole file for "next problem") is
 * turned off; severity-highlighting.ts also stops the worker when one call
 * to the regex engine doesn't return. */
export const HIGHLIGHT_MAX_CHARS = 2000
export const PATTERN_BUDGET_MS = 1000

export interface SeverityMatch {
  matchStart: number
  matchLength: number
  level: SeverityPattern['level']
  highlightLine: boolean
}

export interface LinesMatched {
  /** The matches of each line, in the order of `lines`. */
  matches: SeverityMatch[][]
  /** The ids of the patterns that went over their budget; none of their
   * matches are in `matches`. */
  slow: number[]
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Compiles one pattern into a global, case-insensitive matcher. Unlike
 * Rule's path-oriented glob compiler (backend app/rules.py's `_compile_glob`,
 * anchored and `**`/`*` path-segment-aware), a "glob"-kind severity pattern
 * is matched as a literal substring anywhere in the line -- line-content
 * matching needs "does this text appear", not path globbing, so `*`/`**`
 * aren't given special meaning here. Returns null for an invalid
 * user-supplied regex rather than throwing, so one bad admin-entered
 * pattern doesn't break highlighting for every other pattern. */
function compilePattern(pattern: SeverityPattern): RegExp | null {
  try {
    const source =
      pattern.pattern_kind === 'regex' ? pattern.pattern : escapeRegex(pattern.pattern)
    return new RegExp(source, 'gi')
  } catch {
    return null
  }
}

/** Matches every line against every enabled pattern, one pattern at a time
 * over all the lines, collecting every match (not just the first) so
 * multiple markers on the same line each get highlighted. Only the first
 * HIGHLIGHT_MAX_CHARS of each line are scanned. A pattern whose time over
 * all the lines passes PATTERN_BUDGET_MS stops there and is listed in
 * `slow`. `onPatternStart` is called with each pattern's id before it runs. */
export function matchLines(
  lines: string[],
  patterns: SeverityPattern[],
  onPatternStart?: (patternId: number) => void,
): LinesMatched {
  const matches: SeverityMatch[][] = lines.map(() => [])
  const slow: number[] = []
  for (const pattern of patterns) {
    if (!pattern.enabled) continue
    const regex = compilePattern(pattern)
    if (!regex) continue
    onPatternStart?.(pattern.id)

    const found: [number, SeverityMatch][] = []
    const started = performance.now()
    let overBudget = false
    for (let index = 0; index < lines.length && !overBudget; index++) {
      const line = lines[index]
      const text = line.length > HIGHLIGHT_MAX_CHARS ? line.slice(0, HIGHLIGHT_MAX_CHARS) : line
      regex.lastIndex = 0
      let match: RegExpExecArray | null
      while ((match = regex.exec(text)) !== null) {
        if (performance.now() - started > PATTERN_BUDGET_MS) break
        if (match[0].length === 0) {
          regex.lastIndex += 1
          continue
        }
        found.push([
          index,
          {
            matchStart: match.index,
            matchLength: match[0].length,
            level: pattern.level,
            highlightLine: pattern.highlight_line,
          },
        ])
      }
      overBudget = performance.now() - started > PATTERN_BUDGET_MS
    }
    if (overBudget) {
      slow.push(pattern.id)
      continue
    }
    for (const [index, match] of found) matches[index].push(match)
  }
  return { matches, slow }
}
