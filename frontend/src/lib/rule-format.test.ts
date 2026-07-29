import { describe, expect, it } from 'vitest'
import { parsePatternInput, rulesToRawText, toRawLine } from './rule-format'

describe('toRawLine', () => {
  it('renders an include glob rule as-is', () => {
    expect(toRawLine({ type: 'include', pattern: '**/*.log', pattern_kind: 'glob' })).toBe(
      '**/*.log',
    )
  })

  it('prefixes an exclude rule with !', () => {
    expect(toRawLine({ type: 'exclude', pattern: '**/*.tmp', pattern_kind: 'glob' })).toBe(
      '!**/*.tmp',
    )
  })

  it('prefixes a regex rule pattern with re:', () => {
    expect(toRawLine({ type: 'include', pattern: '^access.*\\.log$', pattern_kind: 'regex' })).toBe(
      're:^access.*\\.log$',
    )
  })

  it('combines exclude and regex prefixes in the right order', () => {
    expect(toRawLine({ type: 'exclude', pattern: '^debug', pattern_kind: 'regex' })).toBe(
      '!re:^debug',
    )
  })
})

describe('parsePatternInput', () => {
  it('treats a plain pattern as glob', () => {
    expect(parsePatternInput('**/*.log')).toEqual({ pattern: '**/*.log', pattern_kind: 'glob' })
  })

  it('strips a re: prefix and marks it regex', () => {
    expect(parsePatternInput('re:^access.*\\.log$')).toEqual({
      pattern: '^access.*\\.log$',
      pattern_kind: 'regex',
    })
  })

  it('round-trips through toRawLine for a regex rule', () => {
    const raw = toRawLine({ type: 'include', pattern: '^a.*', pattern_kind: 'regex' })
    expect(parsePatternInput(raw)).toEqual({ pattern: '^a.*', pattern_kind: 'regex' })
  })
})

describe('rulesToRawText', () => {
  it('joins rules one per line in order', () => {
    const text = rulesToRawText([
      { type: 'include', pattern: '**/*.log', pattern_kind: 'glob' },
      { type: 'exclude', pattern: '**/*.tmp', pattern_kind: 'glob' },
    ])
    expect(text).toBe('**/*.log\n!**/*.tmp')
  })

  it('returns an empty string for no rules', () => {
    expect(rulesToRawText([])).toBe('')
  })
})
