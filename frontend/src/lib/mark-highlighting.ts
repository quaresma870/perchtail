import type { PatternKind } from './types'

/** Notepad++-style "Mark" feature: ad hoc, session-only highlighting of one
 * or more patterns at once, each in its own color -- distinct from the
 * admin-configured severity indicators (severity-highlighting.ts), which are
 * a fixed per-source/global set with a level, not something a viewer types
 * in on the spot. Never persisted (same "pure client-side/session state" as
 * bookmarks in Viewer.svelte's `Tab`). */
export interface MarkPattern {
  id: string
  pattern: string
  pattern_kind: PatternKind
  colorIndex: number
}

export interface MarkMatch {
  matchStart: number
  matchLength: number
  colorIndex: number
}

/** Swatch colors for the toolbar's mark chips, matching the hues (at higher
 * opacity, for dot visibility outside the editor) of the `.cm-mark-N`
 * classes defined in codemirror-theme.ts's darkTheme -- CodeMirror's
 * `EditorView.theme` scopes its generated classes under the editor's own
 * DOM subtree, so they can't be reused directly on a toolbar chip outside
 * it; this is the same palette kept in sync by hand instead. */
export const MARK_SWATCH_COLORS: readonly string[] = [
  'rgba(56, 189, 248, 0.9)',
  'rgba(244, 114, 182, 0.9)',
  'rgba(163, 230, 53, 0.9)',
  'rgba(192, 132, 252, 0.9)',
  'rgba(251, 146, 60, 0.9)',
  'rgba(94, 234, 212, 0.9)',
]

/** Fixed palette cycled through as marks are added -- CSS classes for each
 * index are defined once in codemirror-theme.ts's darkTheme, since the
 * count is bounded and known ahead of time (unlike severity indicators'
 * admin-free-text colors, there's no per-mark color picker here). */
export const MARK_COLOR_COUNT = MARK_SWATCH_COLORS.length

/** Picks the lowest-numbered color index not already in use by an existing
 * mark, so two marks never look identical while any unused color remains
 * (including a freed one, e.g. after removing an earlier mark). Once every
 * color is taken, falls back to cycling by count rather than tracking true
 * usage recency -- good enough once there are more simultaneous marks than
 * colors (a 7th+ mark just reuses a color rather than failing), without
 * needing to remember each color's assignment history. */
export function nextMarkColorIndex(existing: MarkPattern[]): number {
  const used = new Set(existing.map((m) => m.colorIndex))
  for (let i = 0; i < MARK_COLOR_COUNT; i += 1) {
    if (!used.has(i)) return i
  }
  return existing.length % MARK_COLOR_COUNT
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Same "glob kind = literal substring, regex kind = as-is" semantics as
 * severity-highlighting.ts's compilePattern -- a mark is about finding text
 * anywhere in a line, not path globbing, so `*`/`**` aren't special here
 * either. Returns null for an invalid user-typed regex so one bad pattern
 * doesn't break every other active mark. */
function compileMarkPattern(mark: MarkPattern): RegExp | null {
  if (mark.pattern.length === 0) return null
  try {
    const source = mark.pattern_kind === 'regex' ? mark.pattern : escapeRegex(mark.pattern)
    return new RegExp(source, 'gi')
  } catch {
    return null
  }
}

/** Scans one line against every active mark pattern, collecting every
 * occurrence of every pattern (not just the first match per pattern) --
 * same "every occurrence" behavior as severity highlighting. */
export function findMarkMatches(lineText: string, marks: MarkPattern[]): MarkMatch[] {
  const matches: MarkMatch[] = []
  for (const mark of marks) {
    const regex = compileMarkPattern(mark)
    if (!regex) continue

    let match: RegExpExecArray | null
    while ((match = regex.exec(lineText)) !== null) {
      if (match[0].length === 0) {
        regex.lastIndex += 1
        continue
      }
      matches.push({ matchStart: match.index, matchLength: match[0].length, colorIndex: mark.colorIndex })
    }
  }
  return matches
}
