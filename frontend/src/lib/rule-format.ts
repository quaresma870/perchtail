import type { PatternKind, Rule } from './types'

/** Renders one rule as a gitignore-style raw-text line: a leading `!`
 * negates (exclude), and a `re:` prefix on the pattern switches it to regex
 * — the same convention `PUT .../rules/raw` parses server-side (see
 * rules.py), so this must stay a mirror of that format, not just "a"
 * serialization of it. */
export function toRawLine(rule: Pick<Rule, 'type' | 'pattern' | 'pattern_kind'>): string {
  const pattern = rule.pattern_kind === 'regex' ? `re:${rule.pattern}` : rule.pattern
  return rule.type === 'exclude' ? `!${pattern}` : pattern
}

/** Inverse of the pattern half of toRawLine: strips a `re:` prefix from a
 * row-editor pattern field back into (pattern, pattern_kind), matching
 * how `PUT .../rules/raw` distinguishes glob from regex server-side. */
export function parsePatternInput(raw: string): { pattern: string; pattern_kind: PatternKind } {
  if (raw.startsWith('re:')) {
    return { pattern: raw.slice(3), pattern_kind: 'regex' }
  }
  return { pattern: raw, pattern_kind: 'glob' }
}

export function rulesToRawText(rules: Pick<Rule, 'type' | 'pattern' | 'pattern_kind'>[]): string {
  return rules.map(toRawLine).join('\n')
}
