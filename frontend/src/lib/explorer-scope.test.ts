import { describe, expect, it } from 'vitest'
import { coveringScope, covers, nameMatches, toggleScope, type Scope } from './explorer-scope'

const s = (sourceId: number, path: string): Scope => ({ sourceId, path })

describe('scopes', () => {
  it('knows which scope covers which', () => {
    expect(covers(s(1, ''), s(1, 'var/log'))).toBe(true)
    expect(covers(s(1, 'var'), s(1, 'var/log'))).toBe(true)
    expect(covers(s(1, 'var'), s(1, 'variable'))).toBe(false)
    expect(covers(s(1, ''), s(2, 'var'))).toBe(false)
  })

  it('adds a scope, dropping the ones it covers, and removes it again', () => {
    let list = toggleScope([], s(1, 'var/log/app'))
    list = toggleScope(list, s(2, 'logs'))
    list = toggleScope(list, s(1, 'var/log'))
    expect(list).toEqual([s(2, 'logs'), s(1, 'var/log')])
    list = toggleScope(list, s(1, 'var/log'))
    expect(list).toEqual([s(2, 'logs')])
  })

  it("doesn't add a scope already covered from above", () => {
    const list = [s(1, '')]
    expect(toggleScope(list, s(1, 'var'))).toBe(list)
    expect(coveringScope(list, s(1, 'var'))).toEqual(s(1, ''))
    expect(coveringScope(list, s(1, ''))).toBeUndefined()
  })
})

describe('nameMatches', () => {
  it('matches text anywhere, ignoring case', () => {
    expect(nameMatches('App-2026-11-06.log', 'app-2026')).toBe(true)
    expect(nameMatches('app.log', 'error')).toBe(false)
    expect(nameMatches('anything', '  ')).toBe(true)
  })

  it('matches globs against the whole name', () => {
    expect(nameMatches('app-2026-11-06.log', 'app-2026-11*')).toBe(true)
    expect(nameMatches('app-2026-11-06.log', '*.log')).toBe(true)
    expect(nameMatches('app-2026-11-06.log.gz', '*.log')).toBe(false)
    expect(nameMatches('a1.log', 'a?.log')).toBe(true)
    expect(nameMatches('a(1).log', 'a(?).log')).toBe(true)
  })
})
