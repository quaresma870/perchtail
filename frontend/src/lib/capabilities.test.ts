import { describe, expect, it } from 'vitest'
import {
  ALL_CAPABILITIES,
  CAPABILITY_HINTS,
  CAPABILITY_LABELS,
  GLOBAL_CAPABILITY_SHORT_LABELS,
  SCOPE_TYPE_LABELS,
} from './capabilities'
import type { Capability, GlobalCapability, ScopeType } from './types'

const CAPABILITIES: Capability[] = ['view', 'download', 'manage_rules', 'manage_source', 'run_now']
const SCOPE_TYPES: ScopeType[] = ['customer', 'folder', 'source']
const GLOBAL_CAPABILITIES: GlobalCapability[] = [
  'create_source',
  'manage_users',
  'manage_roles',
  'manage_sso',
  'manage_system_settings',
  'view_audit_log',
  'manage_alerts',
]

describe('capability labels', () => {
  it('offers every grant capability once, in order', () => {
    expect(ALL_CAPABILITIES).toEqual(CAPABILITIES)
  })

  it('labels and explains every grant capability', () => {
    for (const cap of CAPABILITIES) {
      expect(CAPABILITY_LABELS[cap].trim()).not.toBe('')
      expect(CAPABILITY_HINTS[cap].trim()).not.toBe('')
    }
  })

  it('gives each grant capability a distinct label', () => {
    const labels = CAPABILITIES.map((cap) => CAPABILITY_LABELS[cap].toLowerCase())
    expect(new Set(labels).size).toBe(labels.length)
    expect(CAPABILITY_LABELS.manage_rules).toBe('Manage rules')
    expect(CAPABILITY_LABELS.manage_source).toBe('Manage sources')
  })

  it('labels every scope type', () => {
    for (const scope of SCOPE_TYPES) expect(SCOPE_TYPE_LABELS[scope].trim()).not.toBe('')
  })

  it('labels every global capability', () => {
    for (const cap of GLOBAL_CAPABILITIES) {
      expect(GLOBAL_CAPABILITY_SHORT_LABELS[cap].trim()).not.toBe('')
    }
  })
})
