import { describe, expect, it } from 'vitest'
import { buildConnectionTree } from './connection-tree'
import type { Source } from './types'

function source(overrides: Partial<Source> & { id: number; name: string }): Source {
  return {
    customer_id: null,
    customer_name: null,
    folder_id: null,
    folder_name: null,
    folder_path: [],
    protocol: 'ssh',
    host: 'h1',
    port: null,
    base_path: '/var/log',
    enabled: true,
    is_system: false,
    rule_count: 0,
    has_credential: true,
    has_agent_token: false,
    agent_connected: false,
    agent_last_seen_at: null,
    search_indexing_enabled: false,
    ...overrides,
  }
}

describe('buildConnectionTree', () => {
  it('groups a source with no folder directly under its customer node', () => {
    const tree = buildConnectionTree([
      source({ id: 1, name: 'app01', customer_id: 5, customer_name: 'Acme' }),
    ])
    expect(tree).toHaveLength(1)
    expect(tree[0]).toMatchObject({ id: 'customer:5', name: 'Acme', children: [] })
    expect(tree[0].sources.map((s) => s.name)).toEqual(['app01'])
  })

  it('nests a source under its full folder path', () => {
    const tree = buildConnectionTree([
      source({
        id: 1,
        name: 'app01',
        customer_id: 5,
        customer_name: 'Acme',
        folder_path: [
          { id: 10, name: 'EU' },
          { id: 11, name: 'Production' },
        ],
      }),
    ])
    const customerNode = tree[0]
    expect(customerNode.sources).toEqual([])
    expect(customerNode.children).toHaveLength(1)
    const euNode = customerNode.children[0]
    expect(euNode).toMatchObject({ id: 'folder:10', name: 'EU' })
    expect(euNode.sources).toEqual([])
    expect(euNode.children).toHaveLength(1)
    const prodNode = euNode.children[0]
    expect(prodNode).toMatchObject({ id: 'folder:11', name: 'Production' })
    expect(prodNode.sources.map((s) => s.name)).toEqual(['app01'])
  })

  it('shares one folder node across two sources under the same path', () => {
    const path = [{ id: 10, name: 'Production' }]
    const tree = buildConnectionTree([
      source({ id: 1, name: 'app01', customer_id: 5, customer_name: 'Acme', folder_path: path }),
      source({ id: 2, name: 'app02', customer_id: 5, customer_name: 'Acme', folder_path: path }),
    ])
    expect(tree).toHaveLength(1)
    expect(tree[0].children).toHaveLength(1)
    expect(tree[0].children[0].sources.map((s) => s.name)).toEqual(['app01', 'app02'])
  })

  it('groups a source with no customer under a shared "No customer" node', () => {
    const tree = buildConnectionTree([
      source({ id: 1, name: 'perchtail.log', is_system: true }),
    ])
    expect(tree).toHaveLength(1)
    expect(tree[0]).toMatchObject({ id: 'unassigned', name: 'No customer' })
    expect(tree[0].sources.map((s) => s.name)).toEqual(['perchtail.log'])
  })

  it('sorts customer nodes, folder nodes, and sources alphabetically', () => {
    const tree = buildConnectionTree([
      source({ id: 1, name: 'zeta', customer_id: 2, customer_name: 'Zebra Corp' }),
      source({ id: 2, name: 'alpha', customer_id: 1, customer_name: 'Acme' }),
      source({ id: 3, name: 'beta', customer_id: 1, customer_name: 'Acme' }),
    ])
    expect(tree.map((n) => n.name)).toEqual(['Acme', 'Zebra Corp'])
    expect(tree[0].sources.map((s) => s.name)).toEqual(['alpha', 'beta'])
  })

  it('returns an empty tree for no sources', () => {
    expect(buildConnectionTree([])).toEqual([])
  })
})
