// The body of a severity indicator's update (Settings → Severity
// indicators). The server checks a pattern it receives as a new one (RE2,
// at most 1000 characters), so the pattern is sent only when it changed:
// an indicator saved before those checks can still be switched off, given
// another level or flag, or deleted without being rewritten.

import type { SeverityPattern } from './types'

/** A pattern as the editor shows and the server takes it: `re:` before a
 * regex. */
export const severityPatternInput = (pattern: Pick<SeverityPattern, 'pattern' | 'pattern_kind'>): string =>
  pattern.pattern_kind === 'regex' ? `re:${pattern.pattern}` : pattern.pattern

export interface SeverityPatternUpdate {
  level: SeverityPattern['level']
  pattern?: string
  enabled: boolean
  highlight_line: boolean
  include_in_navigation: boolean
}

/** `saved` is the pattern as last loaded from the server (undefined when
 * not known, which sends it). */
export function severityUpdatePayload(pattern: SeverityPattern, saved: string | undefined): SeverityPatternUpdate {
  const typed = severityPatternInput(pattern)
  return {
    level: pattern.level,
    ...(typed !== saved ? { pattern: typed } : {}),
    enabled: pattern.enabled,
    highlight_line: pattern.highlight_line,
    include_in_navigation: pattern.include_in_navigation,
  }
}
