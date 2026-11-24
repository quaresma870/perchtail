import { describe, expect, it } from 'vitest'
import {
  canOpenAuditLogPage,
  canViewAuditLog,
  isSettingsPageActive,
  settingsNavGroups,
} from './settings-nav'
import { SETTINGS_ICON_PATHS } from './settings-icon-paths'
import type { CurrentUser, GlobalCapability } from './types'

function user(capabilities: GlobalCapability[] = [], isSuperAdmin = false): CurrentUser {
  return {
    id: 1,
    username: 'alice',
    display_name: null,
    email: null,
    auth_provider: 'local',
    preferences: {},
    role_id: 2,
    active: true,
    must_change_password: false,
    mfa_enabled: true,
    mfa_required: false,
    is_super_admin: isSuperAdmin,
    global_capabilities: capabilities,
  }
}

/** The sidebar as "Group: Page, Page" lines, for readable expectations. */
const outline = (u: CurrentUser | null, auditViewEnabled = true) =>
  settingsNavGroups(u, auditViewEnabled).map(
    (group) => `${group.label}: ${group.pages.map((page) => page.label).join(', ')}`,
  )

describe('settingsNavGroups', () => {
  it('shows a super admin every group and page, in order', () => {
    expect(outline(user([], true))).toEqual([
      'Sources: Sources, Folders, Agents, Source defaults',
      'Access: Users, Roles, Single sign-on, Sign-in security',
      'Content: Redaction, Severity indicators, Explorer and limits',
      'Operations: Alerts, Email, Backups and updates, Monitoring, Import / export',
      'Audit: Audit log, Access review',
    ])
  })

  it('gives every page its route', () => {
    const paths = settingsNavGroups(user([], true), true).flatMap((group) =>
      group.pages.map((page) => page.path),
    )
    expect(paths).toEqual([
      '/settings/sources',
      '/settings/folders',
      '/settings/agents',
      '/settings/source-defaults',
      '/settings/users',
      '/settings/roles',
      '/settings/sso',
      '/settings/sign-in-security',
      '/settings/redaction',
      '/settings/severity-indicators',
      '/settings/explorer',
      '/settings/alerts',
      '/settings/email',
      '/settings/backups',
      '/settings/monitoring',
      '/settings/import-export',
      '/settings/audit-log',
      '/settings/access-review',
    ])
  })

  it('gives every page its icon, each one drawn by SettingsIcon', () => {
    const icons = settingsNavGroups(user([], true), true).flatMap((group) =>
      group.pages.map((page) => `${page.label}: ${page.icon}`),
    )
    expect(icons).toEqual([
      'Sources: server',
      'Folders: folders',
      'Agents: plug-connected',
      'Source defaults: adjustments',
      'Users: users',
      'Roles: shield-lock',
      'Single sign-on: key',
      'Sign-in security: lock',
      'Redaction: eye-off',
      'Severity indicators: alert-triangle',
      'Explorer and limits: gauge',
      'Alerts: bell',
      'Email: mail',
      'Backups and updates: database-export',
      'Monitoring: activity',
      'Import / export: transfer',
      'Audit log: list-search',
      'Access review: user-check',
    ])
    for (const group of settingsNavGroups(user([], true), true)) {
      for (const page of group.pages) {
        expect(Object.keys(SETTINGS_ICON_PATHS)).toContain(page.icon)
        expect(SETTINGS_ICON_PATHS[page.icon].length).toBeGreaterThan(0)
      }
    }
  })

  it('keeps the Audit log page for a super admin with the audit view off', () => {
    expect(outline(user([], true), false)).toContain('Audit: Audit log, Access review')
  })

  it('shows only the Sources group to a user with create_source only', () => {
    expect(outline(user(['create_source']))).toEqual(['Sources: Sources, Folders, Agents'])
  })

  it('shows Sources and Users to a user with manage_users only', () => {
    expect(outline(user(['manage_users']))).toEqual(['Sources: Sources', 'Access: Users'])
  })

  it('shows the Audit log to view_audit_log only while the audit view is on', () => {
    expect(outline(user(['view_audit_log']), true)).toEqual(['Sources: Sources', 'Audit: Audit log'])
    expect(outline(user(['view_audit_log']), false)).toEqual(['Sources: Sources'])
  })

  it('shows the system settings pages to manage_system_settings, but not the super-admin ones', () => {
    const expected = [
      'Sources: Sources, Source defaults',
      'Access: Sign-in security',
      'Content: Severity indicators, Explorer and limits',
      'Operations: Alerts, Backups and updates, Monitoring',
      'Audit: Audit log',
    ]
    expect(outline(user(['manage_system_settings']), true)).toEqual(expected)
    // The audit view's switch lives on the Audit log page, so it stays.
    expect(outline(user(['manage_system_settings']), false)).toEqual(expected)
  })

  it('shows the remaining capabilities their own page', () => {
    expect(outline(user(['manage_roles']))).toEqual(['Sources: Sources', 'Access: Roles'])
    expect(outline(user(['manage_sso']))).toEqual(['Sources: Sources', 'Access: Single sign-on'])
    expect(outline(user(['view_access_report']))).toEqual([
      'Sources: Sources',
      'Audit: Access review',
    ])
    expect(outline(user(['manage_alerts']))).toEqual(['Sources: Sources'])
  })

  it('shows nothing without a user', () => {
    expect(outline(null)).toEqual([])
  })
})

describe('audit log page access', () => {
  it('opens the viewer only with view_audit_log and the audit view on', () => {
    expect(canViewAuditLog(user(['view_audit_log']), true)).toBe(true)
    expect(canViewAuditLog(user(['view_audit_log']), false)).toBe(false)
    expect(canViewAuditLog(user(['manage_system_settings']), true)).toBe(false)
    expect(canViewAuditLog(user([], true), false)).toBe(false)
  })

  it('opens the page for manage_system_settings even with the audit view off', () => {
    expect(canOpenAuditLogPage(user(['manage_system_settings']), false)).toBe(true)
    expect(canOpenAuditLogPage(user(['view_audit_log']), false)).toBe(false)
    expect(canOpenAuditLogPage(user(['manage_users']), true)).toBe(false)
    expect(canOpenAuditLogPage(null, true)).toBe(false)
  })
})

describe('isSettingsPageActive', () => {
  it('matches the page and the pages below it, not a longer name', () => {
    expect(isSettingsPageActive('/settings/sources', '/settings/sources')).toBe(true)
    expect(isSettingsPageActive('/settings/sources/3/edit', '/settings/sources')).toBe(true)
    expect(isSettingsPageActive('/settings/source-defaults', '/settings/sources')).toBe(false)
    expect(isSettingsPageActive('/settings/roles', '/settings/sources')).toBe(false)
  })
})
