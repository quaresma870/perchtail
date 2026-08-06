import type { SeverityPattern } from './types'

export interface SeverityMatch {
  matchStart: number
  matchLength: number
  level: SeverityPattern['level']
  highlightLine: boolean
}

export const LEVEL_CLASS: Record<SeverityPattern['level'], string> = {
  error: 'cm-level-error',
  warning: 'cm-level-warn',
  info: 'cm-level-info',
  debug: 'cm-level-debug',
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

/** Scans one line against every enabled pattern, collecting every match
 * (not just the first) so multiple markers on the same line each get
 * highlighted -- same "every occurrence" behavior the old hardcoded
 * MatchDecorator-based token highlighting had. */
export function findMatchesInLine(lineText: string, patterns: SeverityPattern[]): SeverityMatch[] {
  const matches: SeverityMatch[] = []
  for (const pattern of patterns) {
    if (!pattern.enabled) continue
    const regex = compilePattern(pattern)
    if (!regex) continue

    let match: RegExpExecArray | null
    while ((match = regex.exec(lineText)) !== null) {
      if (match[0].length === 0) {
        regex.lastIndex += 1
        continue
      }
      matches.push({
        matchStart: match.index,
        matchLength: match[0].length,
        level: pattern.level,
        highlightLine: pattern.highlight_line,
      })
    }
  }
  return matches
}

export function lineHasHighlight(lineText: string, patterns: SeverityPattern[]): boolean {
  return findMatchesInLine(lineText, patterns).some((m) => m.highlightLine)
}

/** 1-indexed line numbers containing a match from a navigation-eligible
 * pattern (`include_in_navigation`), for the "next/previous problem" step
 * command -- deliberately a per-pattern flag rather than a fixed
 * warn-or-worse severity floor, so an admin decides what counts as a "step
 * to" problem instead of it being hardcoded. */
export function findProblemLines(content: string, patterns: SeverityPattern[]): number[] {
  const navPatterns = patterns.filter((p) => p.include_in_navigation)
  if (navPatterns.length === 0) return []

  const result: number[] = []
  content.split('\n').forEach((lineText, index) => {
    if (findMatchesInLine(lineText, navPatterns).length > 0) {
      result.push(index + 1)
    }
  })
  return result
}

/** Steps forward from `currentLine` to the next problem line, wrapping
 * around to the first one past the end of the document. `problemLines`
 * must already be sorted ascending (as `findProblemLines` returns it). */
export function nextProblemLine(problemLines: number[], currentLine: number): number | null {
  if (problemLines.length === 0) return null
  return problemLines.find((line) => line > currentLine) ?? problemLines[0]
}

/** Inverse of `nextProblemLine` -- steps backward, wrapping to the last
 * problem line past the start of the document. */
export function previousProblemLine(problemLines: number[], currentLine: number): number | null {
  if (problemLines.length === 0) return null
  for (let i = problemLines.length - 1; i >= 0; i -= 1) {
    if (problemLines[i] < currentLine) return problemLines[i]
  }
  return problemLines[problemLines.length - 1]
}
