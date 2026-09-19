import type { Source } from './types'

/** A customer or folder grouping node in the Viewer's connections-home
 * tree. Deliberately one shared shape for both levels (not separate
 * Customer/Folder node types) -- they render identically (a name, nested
 * groups, and the sources that sit directly at that level), and the only
 * difference between them is where their name comes from. */
export interface ConnectionTreeNode {
  id: string
  name: string
  children: ConnectionTreeNode[]
  sources: Source[]
}

const UNASSIGNED_ID = 'unassigned'
const UNASSIGNED_NAME = 'No customer'

/** Builds the whole customer → folder → … → source tree client-side from
 * an already RBAC-filtered `Source[]` (whatever `GET /sources` /
 * `filterConnections` already returned) -- no separate `/folders` or
 * `/customers` call, and therefore no need for a new RBAC-scoped endpoint:
 * every source here already carries its own `customer_name` and full
 * `folder_path` (root-to-leaf), which is exactly the tree this needs.
 * Folder ids are globally unique (not scoped per customer), so a single
 * flat `nodesById` map is enough to dedupe a folder shared by multiple
 * sources without tracking per-branch lookups separately. */
export function buildConnectionTree(sources: Source[]): ConnectionTreeNode[] {
  const roots: ConnectionTreeNode[] = []
  const nodesById = new Map<string, ConnectionTreeNode>()

  function ensureNode(id: string, name: string, parentChildren: ConnectionTreeNode[]): ConnectionTreeNode {
    let node = nodesById.get(id)
    if (!node) {
      node = { id, name, children: [], sources: [] }
      nodesById.set(id, node)
      parentChildren.push(node)
    }
    return node
  }

  for (const source of sources) {
    const customerId = source.customer_id !== null ? `customer:${source.customer_id}` : UNASSIGNED_ID
    const customerName = source.customer_id !== null ? (source.customer_name ?? 'Unknown customer') : UNASSIGNED_NAME
    let node = ensureNode(customerId, customerName, roots)

    for (const entry of source.folder_path) {
      node = ensureNode(`folder:${entry.id}`, entry.name, node.children)
    }

    node.sources.push(source)
  }

  sortTree(roots)
  return roots
}

function sortTree(nodes: ConnectionTreeNode[]): void {
  nodes.sort((a, b) => a.name.localeCompare(b.name))
  for (const node of nodes) {
    sortTree(node.children)
    node.sources.sort((a, b) => a.name.localeCompare(b.name))
  }
}
