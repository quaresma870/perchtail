import { beforeEach, describe, expect, it } from 'vitest'
import { loadViewState, matchesText, nextSort, saveViewState, sortRows } from './table-view'

type Row = { name: string; rules: number | null; enabled: boolean }
const rows: Row[] = [
  { name: 'app10', rules: 3, enabled: true },
  { name: 'App2', rules: null, enabled: false },
  { name: 'db1', rules: 1, enabled: true },
]
const accessors = {
  name: (r: Row) => r.name,
  rules: (r: Row) => r.rules,
  enabled: (r: Row) => r.enabled,
}
const names = (list: Row[]) => list.map((r) => r.name)

describe('sortRows', () => {
  it('sorts names naturally and ignoring case, both ways', () => {
    expect(names(sortRows(rows, { key: 'name', dir: 'asc' }, accessors))).toEqual([
      'App2',
      'app10',
      'db1',
    ])
    expect(names(sortRows(rows, { key: 'name', dir: 'desc' }, accessors))).toEqual([
      'db1',
      'app10',
      'App2',
    ])
  })

  it('keeps empty values last in either direction', () => {
    expect(names(sortRows(rows, { key: 'rules', dir: 'asc' }, accessors))).toEqual([
      'db1',
      'app10',
      'App2',
    ])
    expect(names(sortRows(rows, { key: 'rules', dir: 'desc' }, accessors))).toEqual([
      'app10',
      'db1',
      'App2',
    ])
  })

  it('is stable and leaves the order alone without a sort or with an unknown key', () => {
    expect(names(sortRows(rows, { key: 'enabled', dir: 'desc' }, accessors))).toEqual([
      'app10',
      'db1',
      'App2',
    ])
    expect(names(sortRows(rows, null, accessors))).toEqual(names(rows))
    expect(names(sortRows(rows, { key: 'nope', dir: 'asc' }, accessors))).toEqual(names(rows))
  })
})

describe('nextSort', () => {
  it('starts ascending and flips on the same column', () => {
    expect(nextSort(null, 'name')).toEqual({ key: 'name', dir: 'asc' })
    expect(nextSort({ key: 'name', dir: 'asc' }, 'name')).toEqual({ key: 'name', dir: 'desc' })
    expect(nextSort({ key: 'name', dir: 'desc' }, 'rules')).toEqual({ key: 'rules', dir: 'asc' })
  })
})

describe('matchesText', () => {
  it('needs every word somewhere in the fields, ignoring case', () => {
    expect(matchesText(['app01', 'Vodacom', null], 'vodacom APP')).toBe(true)
    expect(matchesText(['app01', 'Vodacom'], 'app fidelidade')).toBe(false)
    expect(matchesText(['anything'], '   ')).toBe(true)
  })
})

describe('view state', () => {
  beforeEach(() => sessionStorage.clear())

  it('round-trips and fills in missing fields from the fallback', () => {
    saveViewState('k', { text: 'app' })
    expect(loadViewState('k', { text: '', status: '' })).toEqual({ text: 'app', status: '' })
  })

  it('falls back on corrupt data', () => {
    sessionStorage.setItem('k', '{not json')
    expect(loadViewState('k', { text: '' })).toEqual({ text: '' })
  })
})
