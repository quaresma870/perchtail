import { describe, expect, it } from 'vitest'
import {
  ALL_FLAGS,
  FLAG_LABELS,
  filterSources,
  filterUsers,
  flagHint,
  flagText,
  groupAccess,
  holdersByCapability,
  jsonExportName,
  loginMethod,
  mfaText,
  parseInactiveDays,
  placeText,
  reportUrl,
  viaText,
} from './access-report'
import type {
  AccessReportAccess,
  AccessReportHolder,
  AccessReportSource,
  AccessReportUser,
  AccessReportVia,
} from './types'

const via = (overrides: Partial<AccessReportVia> = {}): AccessReportVia => ({
  principal_type: 'role',
  principal_id: 3,
  principal_name: 'Ops',
  scope_type: 'customer',
  scope_id: 7,
  scope_name: 'Northwind',
  above_your_scope: false,
  ...overrides,
})

const access = (overrides: Partial<AccessReportAccess> = {}): AccessReportAccess => ({
  source_id: 1,
  source_name: 'web-01',
  is_system: false,
  customer_id: 7,
  customer_name: 'Northwind',
  folder_path: [],
  folder_path_truncated: false,
  capabilities: ['view'],
  via: [via()],
  ...overrides,
})

const user = (overrides: Partial<AccessReportUser> = {}): AccessReportUser => ({
  id: 1,
  username: 'alice',
  display_name: 'Alice Doe',
  role: { id: 3, name: 'Ops' },
  auth_provider: 'local',
  sso_role_group: null,
  last_login_at: '2026-11-01T10:00:00Z',
  mfa: 'enabled',
  global_capabilities: [],
  flags: [],
  reaches_all_sources: { capabilities: [], customer_grant_on_every_customer: false },
  access: [],
  ...overrides,
})

const holder = (overrides: Partial<AccessReportHolder> = {}): AccessReportHolder => ({
  id: 1,
  username: 'alice',
  display_name: null,
  role: { id: 3, name: 'Ops' },
  capabilities: ['view'],
  via: [via()],
  ...overrides,
})

const source = (overrides: Partial<AccessReportSource> = {}): AccessReportSource => ({
  source_id: 1,
  source_name: 'web-01',
  is_system: false,
  customer_id: 7,
  customer_name: 'Northwind',
  folder_path: [],
  folder_path_truncated: false,
  users: [],
  ...overrides,
})

describe('flags', () => {
  it('labels every flag', () => {
    for (const flag of ALL_FLAGS) expect(FLAG_LABELS[flag]).toBeTruthy()
  })

  it('words inactivity with the days, or a user who never signed in', () => {
    expect(flagText('inactive', user(), 90)).toBe('No sign-in in 90 days')
    expect(flagText('inactive', user(), 1)).toBe('No sign-in in 1 day')
    expect(flagText('inactive', user({ last_login_at: null }), 90)).toBe('Never signed in')
  })

  it('names the capabilities held on every source', () => {
    const u = user({
      reaches_all_sources: { capabilities: ['view', 'download'], customer_grant_on_every_customer: true },
    })
    expect(flagText('reaches_all_sources', u, 90)).toBe('Reaches every source: View, Download')
    expect(flagHint('reaches_all_sources', u)).toContain('sources added later')
    expect(flagHint('reaches_all_sources', user())).not.toContain('sources added later')
    expect(flagText('local_without_mfa', u, 90)).toBe('Local account without MFA')
  })
})

describe('loginMethod and mfaText', () => {
  it('names the sign-in method and the SSO group when there is one', () => {
    expect(loginMethod(user())).toBe('Local')
    expect(loginMethod(user({ auth_provider: 'oidc' }))).toBe('SSO')
    expect(loginMethod(user({ auth_provider: 'oidc', sso_role_group: 'log-readers' }))).toBe(
      'SSO (group: log-readers)',
    )
  })

  it('words the MFA state', () => {
    expect(mfaText('enabled')).toBe('On')
    expect(mfaText('disabled')).toBe('Off')
    expect(mfaText('idp')).toBe('Identity provider')
  })
})

describe('filterUsers', () => {
  const users = [
    user({ id: 1, username: 'alice', flags: ['local_without_mfa'] }),
    user({ id: 2, username: 'bob', display_name: 'Bob Roe', flags: ['inactive'], role: { id: 4, name: 'Auditors' } }),
    user({ id: 3, username: 'carol', flags: [] }),
  ]

  it('keeps everyone with no flag picked and no query', () => {
    expect(filterUsers(users, new Set(), '').map((u) => u.id)).toEqual([1, 2, 3])
  })

  it('keeps users with any of the picked flags', () => {
    expect(filterUsers(users, new Set(['local_without_mfa']), '').map((u) => u.id)).toEqual([1])
    expect(filterUsers(users, new Set(['local_without_mfa', 'inactive']), '').map((u) => u.id)).toEqual([1, 2])
    expect(filterUsers(users, new Set(['super_admin']), '')).toEqual([])
  })

  it('matches the query on username, display name and role', () => {
    expect(filterUsers(users, new Set(), 'roe').map((u) => u.id)).toEqual([2])
    expect(filterUsers(users, new Set(), 'auditors').map((u) => u.id)).toEqual([2])
    expect(filterUsers(users, new Set(['local_without_mfa']), 'bob')).toEqual([])
  })
})

describe('groupAccess', () => {
  const entries = [
    access({ source_id: 1, source_name: 'web-01', folder_path: ['Prod', 'Web'] }),
    access({ source_id: 2, source_name: 'web-02', folder_path: ['Prod', 'Web'] }),
    access({ source_id: 3, source_name: 'db-01', folder_path: ['Prod'] }),
    access({ source_id: 4, source_name: 'loose', folder_path: [] }),
    access({ source_id: 5, source_name: 'other', customer_id: 8, customer_name: 'Contoso' }),
    access({
      source_id: 6,
      source_name: 'PerchTail',
      is_system: true,
      customer_id: null,
      customer_name: null,
    }),
  ]

  it('groups by customer, then folders, then sources, in the order given', () => {
    const groups = groupAccess(entries)
    expect(groups.map((g) => [g.kind, g.label, g.count])).toEqual([
      ['customer', 'Northwind', 4],
      ['customer', 'Contoso', 1],
      ['system', 'System', 1],
    ])
    const northwind = groups[0]
    expect(northwind.sources.map((s) => s.source_name)).toEqual(['loose'])
    expect(northwind.groups.map((g) => [g.label, g.count])).toEqual([['Prod', 3]])
    const prod = northwind.groups[0]
    expect(prod.sources.map((s) => s.source_name)).toEqual(['db-01'])
    expect(prod.groups.map((g) => g.label)).toEqual(['Web'])
    expect(prod.groups[0].sources.map((s) => s.source_name)).toEqual(['web-01', 'web-02'])
  })

  it('marks a folder path cut at the generator scope', () => {
    const groups = groupAccess([
      access({ folder_path: ['Web'], folder_path_truncated: true }),
      access({ source_id: 2, folder_path: ['Web'], folder_path_truncated: false }),
    ])
    expect(groups[0].groups.map((g) => [g.label, g.truncated])).toEqual([
      ['Web', true],
      ['Web', false],
    ])
  })

  it('keeps only the sources matching the query, by source, customer or folder', () => {
    expect(groupAccess(entries, 'web').flatMap((g) => g.label)).toEqual(['Northwind'])
    expect(groupAccess(entries, 'web')[0].count).toBe(2)
    expect(groupAccess(entries, 'contoso').map((g) => g.label)).toEqual(['Contoso'])
    expect(groupAccess(entries, 'prod')[0].count).toBe(3)
    expect(groupAccess(entries, 'nothing-matches')).toEqual([])
  })
})

describe('sources view', () => {
  it('filters sources by name, customer or folder', () => {
    const sources = [
      source({ source_id: 1, source_name: 'web-01', folder_path: ['Prod'] }),
      source({ source_id: 2, source_name: 'db-01', customer_name: 'Contoso' }),
    ]
    expect(filterSources(sources, 'web').map((s) => s.source_id)).toEqual([1])
    expect(filterSources(sources, 'contoso').map((s) => s.source_id)).toEqual([2])
    expect(filterSources(sources, 'prod').map((s) => s.source_id)).toEqual([1])
    expect(filterSources(sources, '').map((s) => s.source_id)).toEqual([1, 2])
  })

  it('lists who holds each capability, in the role editor order', () => {
    const rows = holdersByCapability(
      source({
        users: [
          holder({ id: 1, username: 'alice', capabilities: ['download', 'view'] }),
          holder({ id: 2, username: 'bob', capabilities: ['view'] }),
        ],
      }),
    )
    expect(rows.map((r) => [r.capability, r.holders.map((h) => h.username)])).toEqual([
      ['view', ['alice', 'bob']],
      ['download', ['alice']],
    ])
  })

  it('writes where a source sits', () => {
    expect(placeText(source({ folder_path: ['Prod', 'Web'] }))).toBe('Northwind / Prod / Web')
    expect(placeText(source({ folder_path: ['Web'], folder_path_truncated: true }))).toBe(
      'Northwind / … / Web',
    )
    expect(placeText(source({ is_system: true, customer_id: null, customer_name: null }))).toBe('System')
  })
})

describe('viaText', () => {
  it('names the role and the scope of its grant', () => {
    expect(viaText(via())).toBe('Role Ops: Customer Northwind')
    expect(viaText(via({ scope_type: 'folder', scope_name: 'Prod' }))).toBe('Role Ops: Folder Prod')
    expect(viaText(via({ scope_type: 'source', scope_name: null, scope_id: 9 }))).toBe('Role Ops: Source #9')
  })

  it('words a super admin and a scope above the generator', () => {
    expect(viaText(via({ scope_type: 'super_admin', scope_id: null, scope_name: null, principal_name: 'Super Admin' }))).toBe(
      'Role Super Admin (super admin)',
    )
    expect(
      viaText(via({ scope_type: 'customer', scope_id: null, scope_name: null, above_your_scope: true })),
    ).toBe('Role Ops: customer above your scope')
  })
})

describe('parseInactiveDays', () => {
  it('accepts whole days between 1 and 3650', () => {
    expect(parseInactiveDays('90')).toBe(90)
    expect(parseInactiveDays(1)).toBe(1)
    expect(parseInactiveDays(' 3650 ')).toBe(3650)
  })

  it('refuses anything else', () => {
    for (const value of ['0', '3651', '-5', '1.5', 'abc', '', null, undefined]) {
      expect(parseInactiveDays(value)).toBeNull()
    }
  })
})

describe('exports', () => {
  it('asks the server for the same view and parameter', () => {
    expect(reportUrl('users', 'csv', 30)).toBe('/access-report?view=users&format=csv&inactive_days=30')
    expect(reportUrl('sources', 'json', 90)).toBe(
      '/access-report?view=sources&format=json&inactive_days=90',
    )
  })

  it('names the JSON file like the CSV, dated in UTC', () => {
    expect(jsonExportName('users', new Date('2026-11-16T23:30:00Z'))).toBe(
      'perchtail-access-report-users-2026-11-16.json',
    )
  })
})
