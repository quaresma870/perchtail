// Settings → SSO, group → role mappings. A delegated admin sees every
// mapping, but not the name of a role beyond their own authority
// (role_outside_authority, role_name null), and the server refuses them
// changing or removing such a mapping or any mapping after it (the last
// match wins, so removing a later one would uncover it: api/sso.py,
// _require_order_within_authority). The server decides; this only keeps
// the page from offering what it would refuse.

import type { GroupRoleMapping } from './types'

export const OUTSIDE_AUTHORITY_ROLE = 'A role outside your access'

export function mappingRoleLabel(mapping: GroupRoleMapping): string {
  return mapping.role_outside_authority ? OUTSIDE_AUTHORITY_ROLE : (mapping.role_name ?? `#${mapping.role_id}`)
}

/** Whether the server would let this admin remove `mapping`: its role is
 * within their authority, and so is every other mapping at or before its
 * position. */
export function canRemoveMapping(mapping: GroupRoleMapping, all: GroupRoleMapping[]): boolean {
  if (mapping.role_outside_authority) return false
  return !all.some((other) => other.id !== mapping.id && other.role_outside_authority && other.order <= mapping.order)
}
