import { describe, expect, it } from 'vitest'
import { filterSourcesByNameOrHost } from './source-match'
import type { Source } from './types'

function makeSource(overrides: Partial<Source>): Source {
  return {
    id: 1,
    name: 'source',
    customer_id: null,
    customer_name: null,
    folder_id: null,
    folder_name: null,
    protocol: 'ssh',
    host: 'host.example.com',
    port: null,
    base_path: '/var/log',
    enabled: true,
    is_system: false,
    rule_count: 0,
    has_credential: false,
    has_agent_token: false,
    agent_connected: false,
    agent_last_seen_at: null,
    search_indexing_enabled: false,
    ...overrides,
  }
}

describe('filterSourcesByNameOrHost', () => {
  const sources = [
    makeSource({ id: 1, name: 'app01', host: 'app01.prod.example.com' }),
    makeSource({ id: 2, name: 'win-app-02', host: '10.0.0.5' }),
    makeSource({ id: 3, name: 'billing', host: 'win-app-02.internal' }),
  ]

  it('returns nothing for a blank query', () => {
    expect(filterSourcesByNameOrHost(sources, '')).toEqual([])
    expect(filterSourcesByNameOrHost(sources, '   ')).toEqual([])
  })

  it('matches by source display name, case-insensitively', () => {
    expect(filterSourcesByNameOrHost(sources, 'WIN-APP-02')).toEqual([sources[1], sources[2]])
  })

  it('matches by host even when the name differs', () => {
    expect(filterSourcesByNameOrHost(sources, 'internal')).toEqual([sources[2]])
  })

  it('returns an empty list when nothing matches', () => {
    expect(filterSourcesByNameOrHost(sources, 'nonexistent')).toEqual([])
  })
})
