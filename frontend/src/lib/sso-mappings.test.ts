import { describe, expect, it } from 'vitest'
import { canRemoveMapping, mappingRoleLabel } from './sso-mappings'
import type { GroupRoleMapping } from './types'

const mapping = (id: number, order: number, outside = false): GroupRoleMapping => ({
  id,
  order,
  group_name: `group-${id}`,
  role_id: 10 + id,
  role_name: outside ? null : `Role ${id}`,
  role_outside_authority: outside,
})

describe('mappingRoleLabel', () => {
  it("names the role, or says it's outside the admin's access", () => {
    expect(mappingRoleLabel(mapping(1, 0))).toBe('Role 1')
    expect(mappingRoleLabel(mapping(2, 1, true))).toBe('A role outside your access')
  })
})

describe('canRemoveMapping', () => {
  const all = [mapping(1, 0), mapping(2, 1, true), mapping(3, 2)]

  it('offers removal of a mapping within authority with nothing beyond it before', () => {
    expect(canRemoveMapping(all[0], all)).toBe(true)
    expect(canRemoveMapping(mapping(4, 0), [mapping(4, 0), mapping(5, 3, true)])).toBe(true)
  })

  it("doesn't offer removal of a mapping to a role outside the admin's access", () => {
    expect(canRemoveMapping(all[1], all)).toBe(false)
  })

  it("doesn't offer removal of a mapping after one to a role outside the admin's access", () => {
    // The server refuses it: removing it would uncover the earlier mapping.
    expect(canRemoveMapping(all[2], all)).toBe(false)
  })
})
