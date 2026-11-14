import type { Capability, GlobalCapability, ScopeType } from './types'

// Display labels for the role screens. The keys stay as they are in the API,
// the import/export YAML, the audit log and the database; only what the
// roles list and editor show changes. Full `Record`s, so a new key without a
// label fails `svelte-check`.

/** Grant capabilities, in the order the role editor offers them. */
export const ALL_CAPABILITIES: Capability[] = [
  'view',
  'download',
  'manage_rules',
  'manage_source',
  'run_now',
]

export const CAPABILITY_LABELS: Record<Capability, string> = {
  view: 'View',
  download: 'Download',
  manage_rules: 'Manage rules',
  manage_source: 'Manage sources',
  run_now: 'Run now',
}

// Viewing a file necessarily sends its full content to the browser, so
// `view` can read whole files; `download` gates bulk export on top of that.
export const CAPABILITY_HINTS: Record<Capability, string> = {
  view: 'Browse and open files in the viewer. Opening a file sends its full content; every open is audited.',
  download: 'Download whole files and folders as ZIP (bulk export). Every download is audited.',
  manage_rules: "Edit this scope's visibility rules.",
  manage_source:
    'Manage sources: edit, move and delete sources and folders here, their agent and credentials; with "Create new sources", add them here.',
  run_now: 'Reserved: not used by any feature yet.',
}

export const SCOPE_TYPE_LABELS: Record<ScopeType, string> = {
  customer: 'Customer',
  folder: 'Folder',
  source: 'Source',
}

/** Short global-capability labels, for the roles list. */
export const GLOBAL_CAPABILITY_SHORT_LABELS: Record<GlobalCapability, string> = {
  create_source: 'Create sources',
  manage_users: 'Manage users',
  manage_roles: 'Manage roles',
  manage_sso: 'Manage SSO',
  manage_system_settings: 'Manage system settings',
  view_audit_log: 'View audit log',
  manage_alerts: 'Manage alerts',
}
