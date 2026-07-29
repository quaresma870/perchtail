import { describe, expect, it } from 'vitest'
import { classForLevelToken, isErrorLine, LEVEL_TOKEN } from './codemirror-theme'

describe('classForLevelToken', () => {
  it('maps each known level to its CSS class', () => {
    expect(classForLevelToken('info')).toBe('cm-level-info')
    expect(classForLevelToken('warn')).toBe('cm-level-warn')
    expect(classForLevelToken('warning')).toBe('cm-level-warn')
    expect(classForLevelToken('error')).toBe('cm-level-error')
    expect(classForLevelToken('fatal')).toBe('cm-level-error')
    expect(classForLevelToken('debug')).toBe('cm-level-debug')
    expect(classForLevelToken('trace')).toBe('cm-level-debug')
  })

  it('is case-insensitive', () => {
    expect(classForLevelToken('ERROR')).toBe('cm-level-error')
    expect(classForLevelToken('Warn')).toBe('cm-level-warn')
  })
})

describe('LEVEL_TOKEN', () => {
  it('matches bracketed level tokens in a log line', () => {
    const line = '2026-07-29T10:00:00Z [error] connection refused'
    const matches = [...line.matchAll(LEVEL_TOKEN)]
    expect(matches).toHaveLength(1)
    expect(matches[0][1]).toBe('error')
  })

  it('does not match a level word without brackets', () => {
    const line = 'error: connection refused'
    expect([...line.matchAll(LEVEL_TOKEN)]).toHaveLength(0)
  })

  it('matches multiple tokens in the same line', () => {
    const line = '[warn] retrying after [error] from upstream'
    const matches = [...line.matchAll(LEVEL_TOKEN)]
    expect(matches.map((m) => m[1])).toEqual(['warn', 'error'])
  })
})

describe('isErrorLine', () => {
  it('flags lines containing "error" or "fatal" as whole words', () => {
    expect(isErrorLine('2026-07-29 [error] disk full')).toBe(true)
    expect(isErrorLine('fatal: repository not found')).toBe(true)
  })

  it('is case-insensitive', () => {
    expect(isErrorLine('ERROR: something broke')).toBe(true)
  })

  it('does not flag unrelated words that merely contain the substring', () => {
    expect(isErrorLine('terrorism awareness training log')).toBe(false)
  })

  it('does not flag lines with no error/fatal word', () => {
    expect(isErrorLine('2026-07-29 [info] request completed in 12ms')).toBe(false)
  })
})
