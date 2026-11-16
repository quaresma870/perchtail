// Display helpers for Settings -> Access review (#384). The report itself,
// and what it may show, comes from the server (GET /access-report,
// backend/app/access_report.py); nothing here adds to or widens it. These
// only group, filter and word what the API sent.

import { ALL_CAPABILITIES, CAPABILITY_LABELS, SCOPE_TYPE_LABELS } from './capabilities'
import { matchesText } from './table-view'
import type {
  AccessReportAccess,
  AccessReportFlag,
  AccessReportHolder,
  AccessReportPlace,
  AccessReportSource,
  AccessReportUser,
  AccessReportVia,
  AccessReportView,
  Capability,
} from './types'

// Same bounds as the API (backend/app/access_report.py).
export const DEFAULT_INACTIVE_DAYS = 90
export const MIN_INACTIVE_DAYS = 1
export const MAX_INACTIVE_DAYS = 3650

/** Flags in the order they are offered as filters. */
export const ALL_FLAGS: AccessReportFlag[] = [
  'super_admin',
  'global_capabilities',
  'local_without_mfa',
  'must_change_password',
  'inactive',
  'reaches_all_sources',
]

/** Short flag names, for the filters. A full `Record`, so a new flag
 * without a label fails `svelte-check`. */
export const FLAG_LABELS: Record<AccessReportFlag, string> = {
  super_admin: 'Super admin',
  global_capabilities: 'Global capabilities',
  local_without_mfa: 'Local account without MFA',
  must_change_password: 'Must change password',
  inactive: 'Inactive',
  reaches_all_sources: 'Reaches every source',
}

export const FLAG_HINTS: Record<AccessReportFlag, string> = {
  super_admin: 'Bypasses every grant and reaches the system log source.',
  global_capabilities: 'A privileged account: its role holds global capabilities.',
  local_without_mfa: 'Signs in with a local password and has no two-factor authentication.',
  must_change_password: 'Still has to change a temporary password.',
  inactive: 'Has not signed in within the number of days set above.',
  reaches_all_sources: 'Holds these capabilities on every source in this report.',
}

/** The text of one flag on one user, with what makes it specific. */
export function flagText(
  flag: AccessReportFlag,
  user: Pick<AccessReportUser, 'last_login_at' | 'reaches_all_sources'>,
  inactiveDays: number,
): string {
  if (flag === 'inactive') {
    return user.last_login_at === null
      ? 'Never signed in'
      : `No sign-in in ${inactiveDays} ${inactiveDays === 1 ? 'day' : 'days'}`
  }
  if (flag === 'reaches_all_sources') {
    const caps = user.reaches_all_sources.capabilities.map((c) => CAPABILITY_LABELS[c])
    return caps.length ? `${FLAG_LABELS[flag]}: ${caps.join(', ')}` : FLAG_LABELS[flag]
  }
  return FLAG_LABELS[flag]
}

export function flagHint(
  flag: AccessReportFlag,
  user: Pick<AccessReportUser, 'reaches_all_sources'>,
): string {
  if (flag === 'reaches_all_sources' && user.reaches_all_sources.customer_grant_on_every_customer) {
    return `${FLAG_HINTS[flag]} Customer grants cover every customer, so sources added later are reached too.`
  }
  return FLAG_HINTS[flag]
}

/** How the user signs in, naming the identity-provider group that gave
 * the role when there is one. */
export function loginMethod(user: Pick<AccessReportUser, 'auth_provider' | 'sso_role_group'>): string {
  if (user.auth_provider === 'local') return 'Local'
  return user.sso_role_group ? `SSO (group: ${user.sso_role_group})` : 'SSO'
}

export function mfaText(mfa: AccessReportUser['mfa']): string {
  if (mfa === 'enabled') return 'On'
  if (mfa === 'disabled') return 'Off'
  return 'Identity provider'
}

/** Users with any of `flags` (all users when none is picked) whose
 * username, display name or role matches `query`. */
export function filterUsers(
  users: AccessReportUser[],
  flags: Set<AccessReportFlag>,
  query: string,
): AccessReportUser[] {
  return users.filter(
    (user) =>
      (flags.size === 0 || user.flags.some((flag) => flags.has(flag))) &&
      matchesText([user.username, user.display_name, user.role.name], query),
  )
}

function placeMatches(place: AccessReportPlace, query: string): boolean {
  return matchesText([place.source_name, place.customer_name, ...place.folder_path], query)
}

/** Sources whose name, customer or folders match `query`. */
export function filterSources(sources: AccessReportSource[], query: string): AccessReportSource[] {
  return sources.filter((source) => placeMatches(source, query))
}

/** Where a source sits: customer, folders (with "…" where the path is cut
 * at the generator's scope). */
export function placeText(place: AccessReportPlace): string {
  const parts: string[] = []
  if (place.customer_name !== null) parts.push(place.customer_name)
  else if (place.is_system) parts.push('System')
  if (place.folder_path_truncated) parts.push('…')
  parts.push(...place.folder_path)
  return parts.join(' / ')
}

/** One customer or folder of a user's access tree, with the sources
 * directly in it and the folders below it. */
export interface AccessGroup {
  key: string
  kind: 'customer' | 'folder' | 'system'
  label: string
  /** The folders above this one are outside the generator's scope. */
  truncated: boolean
  groups: AccessGroup[]
  sources: AccessReportAccess[]
  /** Sources in this group and every group below it. */
  count: number
}

function newGroup(key: string, kind: AccessGroup['kind'], label: string, truncated = false): AccessGroup {
  return { key, kind, label, truncated, groups: [], sources: [], count: 0 }
}

/** A user's access grouped by customer, then folder, then source, keeping
 * the API's order. With `query`, only the sources whose name, customer or
 * folders match it. */
export function groupAccess(access: AccessReportAccess[], query = ''): AccessGroup[] {
  const top: AccessGroup[] = []
  const byKey = new Map<string, AccessGroup>()

  function child(parent: AccessGroup | null, group: AccessGroup): AccessGroup {
    const existing = byKey.get(group.key)
    if (existing) return existing
    byKey.set(group.key, group)
    if (parent) parent.groups.push(group)
    else top.push(group)
    return group
  }

  for (const entry of access) {
    if (!placeMatches(entry, query)) continue
    let group =
      entry.customer_id === null
        ? child(null, newGroup('system', 'system', 'System'))
        : child(null, newGroup(`c:${entry.customer_id}`, 'customer', entry.customer_name ?? ''))
    const chain = [group]
    entry.folder_path.forEach((name, index) => {
      const truncated = index === 0 && entry.folder_path_truncated
      const key = `${group.key}/${truncated ? '…/' : ''}f:${name}`
      group = child(group, newGroup(key, 'folder', name, truncated))
      chain.push(group)
    })
    group.sources.push(entry)
    for (const g of chain) g.count += 1
  }
  return top
}

/** Who holds each capability on a source, in the role editor's order,
 * leaving out capabilities nobody holds. */
export function holdersByCapability(
  source: Pick<AccessReportSource, 'users'>,
): { capability: Capability; holders: AccessReportHolder[] }[] {
  return ALL_CAPABILITIES.map((capability) => ({
    capability,
    holders: source.users.filter((user) => user.capabilities.includes(capability)),
  })).filter((row) => row.holders.length > 0)
}

/** What gave an access: the role and the scope of its grant. */
export function viaText(via: AccessReportVia): string {
  const who = via.principal_type === 'role' ? `Role ${via.principal_name}` : via.principal_name
  if (via.scope_type === 'super_admin') return `${who} (super admin)`
  const scope = SCOPE_TYPE_LABELS[via.scope_type]
  if (via.above_your_scope) return `${who}: ${scope.toLowerCase()} above your scope`
  return `${who}: ${scope} ${via.scope_name ?? `#${via.scope_id}`}`
}

/** The integer number of days, or null when it is outside what the API
 * accepts. */
export function parseInactiveDays(value: unknown): number | null {
  const text = String(value ?? '').trim()
  if (!/^\d+$/.test(text)) return null
  const days = Number(text)
  return days >= MIN_INACTIVE_DAYS && days <= MAX_INACTIVE_DAYS ? days : null
}

/** The API address of the report, for the page and for the exports, so an
 * export is generated (and audited) by the server, never built in the
 * browser from what is on screen. */
export function reportUrl(view: AccessReportView, format: 'json' | 'csv', inactiveDays: number): string {
  const params = new URLSearchParams({
    view,
    format,
    inactive_days: String(inactiveDays),
  })
  return `/access-report?${params.toString()}`
}

/** The name the JSON export is saved under, like the CSV's (the server
 * names that one), dated in UTC. */
export function jsonExportName(view: AccessReportView, now: Date = new Date()): string {
  return `perchtail-access-report-${view}-${now.toISOString().slice(0, 10)}.json`
}
