import { describe, expect, it } from 'vitest'
import type { ConnectionTreeNode } from './connection-tree'
import {
  compileMatcher,
  contextFromTree,
  coveringScope,
  covers,
  isMatcherError,
  nameMatches,
  parseQuery,
  sourceScopesOf,
  toggleScope,
  type GroupScope,
  type Scope,
} from './explorer-scope'
import type { Source } from './types'

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

// #284: customers and folders of sources as scopes; regex, match case, paths.
const src = (id: number) => ({ id, name: `s${id}` }) as Source
const tree: ConnectionTreeNode[] = [
  {
    id: 'customer:1',
    name: 'Acme',
    sources: [src(1)],
    children: [{ id: 'folder:7', name: 'Prod', sources: [src(2), src(3)], children: [] }],
  },
  { id: 'customer:2', name: 'Other', sources: [src(4)], children: [] },
]
const ctx = contextFromTree(tree)
const g = (groupId: string, name = groupId): GroupScope => ({ groupId, name })

describe('group scopes', () => {
  it('a customer covers its folders, their sources and folders inside them', () => {
    expect(covers(g('customer:1'), g('folder:7'), ctx)).toBe(true)
    expect(covers(g('customer:1'), s(2, ''), ctx)).toBe(true)
    expect(covers(g('customer:1'), s(3, 'var/log'), ctx)).toBe(true)
    expect(covers(g('folder:7'), s(1, ''), ctx)).toBe(false)
    expect(covers(g('customer:2'), s(1, ''), ctx)).toBe(false)
    expect(covers(s(1, ''), g('customer:1'), ctx)).toBe(false)
  })

  it('ticking a customer drops what it covers; covered rows can’t be ticked', () => {
    let list = toggleScope([], s(2, 'logs'), ctx)
    list = toggleScope(list, g('folder:7'), ctx)
    list = toggleScope(list, s(4, ''), ctx)
    expect(list).toEqual([g('folder:7'), s(4, '')])
    list = toggleScope(list, g('customer:1'), ctx)
    expect(list).toEqual([s(4, ''), g('customer:1')])
    expect(coveringScope(list, s(3, ''), ctx)).toEqual(g('customer:1'))
    expect(toggleScope(list, g('folder:7'), ctx)).toBe(list)
  })

  it('stands for every source under it, in tree order', () => {
    expect(sourceScopesOf(g('customer:1'), ctx)).toEqual([s(2, ''), s(3, ''), s(1, '')])
    expect(sourceScopesOf(s(4, 'x'), ctx)).toEqual([s(4, 'x')])
    expect(sourceScopesOf(g('gone'), ctx)).toEqual([])
  })
})

describe('compileMatcher', () => {
  const match = (query: string, name: string, path = name, regex = false, matchCase = false) => {
    const m = compileMatcher(query, { regex, matchCase })
    if (isMatcherError(m)) throw new Error(m.error)
    return m(name, path)
  }

  it('regex is found anywhere in the name, ignoring case unless asked', () => {
    expect(match('app-\\d{4}', 'APP-2026.log', undefined, true)).toBe(true)
    expect(match('app-\\d{4}', 'APP-2026.log', undefined, true, true)).toBe(false)
    expect(match('\\.gz$', 'a.log.gz', undefined, true)).toBe(true)
    expect(match('\\.gz$', 'a.gz.log', undefined, true)).toBe(false)
  })

  it('match case applies to text and globs too', () => {
    expect(match('App', 'app.log', undefined, false, true)).toBe(false)
    expect(match('*.LOG', 'app.log', undefined, false, true)).toBe(false)
    expect(match('*.LOG', 'app.log')).toBe(true)
  })

  it('a pattern with "/" matches the relative path', () => {
    expect(match('app/*.log', 'x.log', 'app/x.log')).toBe(true)
    expect(match('app/*.log', 'x.log', 'other/app/x.log')).toBe(false)
    expect(match('**/2026-11/*.gz', 'a.gz', 'srv1/2026-11/a.gz')).toBe(true)
    expect(match('*/x.log', 'x.log', 'a/b/x.log')).toBe(false)
    expect(match('app/.*\\.log$', 'x.log', 'app/x.log', true)).toBe(true)
    expect(match('/old/', 'x.log', 'archive/old/x.log')).toBe(true)
  })

  it('an invalid regex is an error, not an exception', () => {
    expect(compileMatcher('app(', { regex: true, matchCase: false })).toEqual({ error: 'Invalid regex' })
    expect(isMatcherError(compileMatcher('app(', { regex: false, matchCase: false }))).toBe(false)
  })
})

describe('parseQuery', () => {
  it('reads a regex between slashes, and anything else as text or glob', () => {
    expect(parseQuery('/app\.log\.\d/')).toEqual({ text: 'app\.log\.\d', regex: true })
    expect(parseQuery('  app-*.log ')).toEqual({ text: 'app-*.log', regex: false })
    expect(parseQuery('var/log')).toEqual({ text: 'var/log', regex: false })
    expect(parseQuery('/')).toEqual({ text: '/', regex: false })
  })
})
