// Display helpers for the audit log table (#239): a readable target instead
// of a type and a bare id, and the metadata as key/value pairs.

import type { AuditLogEntry } from './types'

const TARGET_TYPE_LABELS: Record<string, string> = {
  source: 'Source',
  customer: 'Customer',
  folder: 'Folder',
  user: 'User',
  role: 'Role',
  role_grant: 'Grant',
  rule: 'Rule',
  severity_pattern: 'Severity pattern',
  alert: 'Alert',
  sso_provider: 'SSO provider',
  sso_group_mapping: 'SSO group mapping',
  system_settings: 'System settings',
  monitoring_token: 'Monitoring token',
  audit_log: 'Audit log',
}

export function targetTypeLabel(targetType: string): string {
  return TARGET_TYPE_LABELS[targetType] ?? targetType
}

// Where an existing target can be opened; null when it's gone or has no page.
export function targetHref(entry: AuditLogEntry): string | null {
  if (!entry.target_exists || entry.target_id === null) return null
  switch (entry.target_type) {
    case 'source':
      return `#/settings/sources/${entry.target_id}`
    case 'role':
      return `#/settings/roles/${entry.target_id}`
    case 'user':
      return '#/settings/users'
    case 'folder':
    case 'customer':
      return '#/settings/folders'
    case 'alert':
      return '#/alerts'
    default:
      return null
  }
}

// The target's name and context are shown in their own column.
const SHOWN_AS_TARGET = new Set(['target_name', 'target_context'])

export function detailPairs(metadata: Record<string, unknown> | null): [string, string][] {
  if (!metadata) return []
  return Object.entries(metadata)
    .filter(([key]) => !SHOWN_AS_TARGET.has(key))
    .map(([key, value]) => [key, typeof value === 'string' ? value : JSON.stringify(value)])
}
