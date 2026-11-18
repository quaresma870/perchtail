import { describe, expect, it } from 'vitest'
import { severityPatternInput, severityUpdatePayload } from './severity-pattern-update'
import type { SeverityPattern } from './types'

const saved: SeverityPattern = {
  id: 1,
  source_id: null,
  level: 'error',
  // Saved before the RE2 check: lookaround the server now refuses.
  pattern: 'ERROR(?!: ignored)',
  pattern_kind: 'regex',
  enabled: true,
  highlight_line: false,
  include_in_navigation: true,
}

describe('severityUpdatePayload', () => {
  it('leaves the pattern out when only a flag or the level changed', () => {
    const payload = severityUpdatePayload({ ...saved, enabled: false, level: 'warning' }, severityPatternInput(saved))
    expect(payload).toEqual({ level: 'warning', enabled: false, highlight_line: false, include_in_navigation: true })
    expect(payload).not.toHaveProperty('pattern')
  })

  it('sends the pattern, as typed, when it changed', () => {
    const edited: SeverityPattern = { ...saved, pattern: 'FATAL', pattern_kind: 'glob' }
    expect(severityUpdatePayload(edited, severityPatternInput(saved)).pattern).toBe('FATAL')
    const regex: SeverityPattern = { ...saved, pattern: '\bFATAL\b' }
    expect(severityUpdatePayload(regex, severityPatternInput(saved)).pattern).toBe('re:\bFATAL\b')
  })

  it('sends the pattern when what was saved is not known', () => {
    expect(severityUpdatePayload(saved, undefined).pattern).toBe('re:ERROR(?!: ignored)')
  })
})
