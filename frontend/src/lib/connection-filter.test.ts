import { describe, expect, it } from 'vitest'
import { filterConnections } from './connection-filter'
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

describe('filterConnections', () => {
  const sources = [
    makeSource({ id: 1, name: 'app01', host: 'app01.prod.example.com', customer_name: 'Acme Corp', folder_name: 'Production' }),
    makeSource({ id: 2, name: 'app02', host: 'app02.staging.example.com', customer_name: 'Acme Corp', folder_name: 'Staging' }),
    makeSource({ id: 3, name: 'win-app-02', host: 'win-app-02', customer_name: 'Globex', folder_name: null }),
  ]

  it('returns every source when the query is blank', () => {
    expect(filterConnections(sources, '')).toEqual(sources)
    expect(filterConnections(sources, '   ')).toEqual(sources)
  })

  it('matches by customer name, case-insensitively', () => {
    expect(filterConnections(sources, 'acme')).toEqual([sources[0], sources[1]])
  })

  it('matches by folder name', () => {
    expect(filterConnections(sources, 'production')).toEqual([sources[0]])
  })

  it('matches by host, including a source with no folder', () => {
    expect(filterConnections(sources, 'win-app-02')).toEqual([sources[2]])
  })

  it('does not match against the source display name', () => {
    const named = makeSource({
      id: 4,
      name: 'special-display-name',
      host: '10.0.0.5',
      customer_name: 'Foo',
      folder_name: 'Bar',
    })
    expect(filterConnections([named], 'special-display-name')).toEqual([])
  })

  it('returns an empty list when nothing matches', () => {
    expect(filterConnections(sources, 'nonexistent')).toEqual([])
  })
})
