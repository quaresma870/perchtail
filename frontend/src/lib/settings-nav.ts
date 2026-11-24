import type { SettingsIconName } from './settings-icon-paths'
import type { CurrentUser, GlobalCapability } from './types'

// The Settings sidebar: pages grouped in sections. Each page is shown only to
// whoever may use it; the server decides on every request anyway. A group
// with no page for the user isn't shown.

export interface SettingsPage {
  label: string
  path: string
  /** The icon shown before the page's name in the sidebar. */
  icon: SettingsIconName
}

export interface SettingsGroup {
  label: string
  pages: SettingsPage[]
}

type Visibility = (user: CurrentUser, auditViewEnabled: boolean) => boolean

interface PageDefinition extends SettingsPage {
  visible: Visibility
}

const has =
  (capability: GlobalCapability): Visibility =>
  (user) =>
    user.is_super_admin || user.global_capabilities.includes(capability)
const everyone: Visibility = () => true
const superAdmin: Visibility = (user) => user.is_super_admin

/** Whoever may read the audit log, while the page is turned on. */
export function canViewAuditLog(user: CurrentUser | null, auditViewEnabled: boolean): boolean {
  return !!user && auditViewEnabled && has('view_audit_log')(user, auditViewEnabled)
}

/** Whoever manages system settings, who sets the audit log up on its page. */
export function canManageSystemSettings(user: CurrentUser | null): boolean {
  return !!user && has('manage_system_settings')(user, true)
}

/** The Audit log page: its viewer, its settings, or both. */
export function canOpenAuditLogPage(user: CurrentUser | null, auditViewEnabled: boolean): boolean {
  return canViewAuditLog(user, auditViewEnabled) || canManageSystemSettings(user)
}

const GROUPS: { label: string; pages: PageDefinition[] }[] = [
  {
    label: 'Sources',
    pages: [
      { label: 'Sources', path: '/settings/sources', icon: 'server', visible: everyone },
      {
        label: 'Folders',
        path: '/settings/folders',
        icon: 'folders',
        visible: has('create_source'),
      },
      {
        label: 'Agents',
        path: '/settings/agents',
        icon: 'plug-connected',
        visible: has('create_source'),
      },
      {
        label: 'Source defaults',
        path: '/settings/source-defaults',
        icon: 'adjustments',
        visible: has('manage_system_settings'),
      },
    ],
  },
  {
    label: 'Access',
    pages: [
      { label: 'Users', path: '/settings/users', icon: 'users', visible: has('manage_users') },
      {
        label: 'Roles',
        path: '/settings/roles',
        icon: 'shield-lock',
        visible: has('manage_roles'),
      },
      { label: 'Single sign-on', path: '/settings/sso', icon: 'key', visible: has('manage_sso') },
      {
        label: 'Sign-in security',
        path: '/settings/sign-in-security',
        icon: 'lock',
        visible: has('manage_system_settings'),
      },
    ],
  },
  {
    label: 'Content',
    pages: [
      { label: 'Redaction', path: '/settings/redaction', icon: 'eye-off', visible: superAdmin },
      {
        label: 'Severity indicators',
        path: '/settings/severity-indicators',
        icon: 'alert-triangle',
        visible: has('manage_system_settings'),
      },
      {
        label: 'Explorer and limits',
        path: '/settings/explorer',
        icon: 'gauge',
        visible: has('manage_system_settings'),
      },
    ],
  },
  {
    label: 'Operations',
    pages: [
      {
        label: 'Alerts',
        path: '/settings/alerts',
        icon: 'bell',
        visible: has('manage_system_settings'),
      },
      { label: 'Email', path: '/settings/email', icon: 'mail', visible: superAdmin },
      {
        label: 'Backups and updates',
        path: '/settings/backups',
        icon: 'database-export',
        visible: has('manage_system_settings'),
      },
      {
        label: 'Monitoring',
        path: '/settings/monitoring',
        icon: 'activity',
        visible: has('manage_system_settings'),
      },
      {
        label: 'Import / export',
        path: '/settings/import-export',
        icon: 'transfer',
        visible: superAdmin,
      },
    ],
  },
  {
    label: 'Audit',
    pages: [
      {
        label: 'Audit log',
        path: '/settings/audit-log',
        icon: 'list-search',
        visible: (user, enabled) => canOpenAuditLogPage(user, enabled),
      },
      {
        label: 'Access review',
        path: '/settings/access-review',
        icon: 'user-check',
        visible: has('view_access_report'),
      },
    ],
  },
]

/** The sidebar's groups and pages for this user, in order, empty groups left out. */
export function settingsNavGroups(
  user: CurrentUser | null,
  auditViewEnabled: boolean,
): SettingsGroup[] {
  if (!user) return []
  return GROUPS.map((group) => ({
    label: group.label,
    pages: group.pages
      .filter((page) => page.visible(user, auditViewEnabled))
      .map(({ label, path, icon }) => ({ label, path, icon })),
  })).filter((group) => group.pages.length > 0)
}

/** Whether `hash` (without the leading "#") is `path` or a page below it. */
export function isSettingsPageActive(hash: string, path: string): boolean {
  return hash === path || hash.startsWith(path + '/')
}
