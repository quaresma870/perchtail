export type Protocol = 'ssh' | 'smb' | 'winrm' | 'local' | 'agent'
export type RuleType = 'include' | 'exclude'
export type PatternKind = 'glob' | 'regex'
export type ScopeType = 'customer' | 'folder' | 'source'
export type SeverityLevel = 'error' | 'warning' | 'info' | 'debug'
export type Capability = 'view' | 'download' | 'manage_rules' | 'run_now'
export type GlobalCapability =
  | 'manage_users'
  | 'manage_roles'
  | 'manage_sso'
  | 'create_source'
  | 'manage_system_settings'

export interface CurrentUser {
  id: number
  username: string
  role_id: number
  active: boolean
  must_change_password: boolean
  is_super_admin: boolean
  global_capabilities: GlobalCapability[]
}

export interface Customer {
  id: number
  name: string
}

export interface Folder {
  id: number
  name: string
  customer_id: number
  parent_folder_id: number | null
}

export interface Source {
  id: number
  name: string
  customer_id: number | null
  customer_name: string | null
  folder_id: number | null
  folder_name: string | null
  protocol: Protocol
  host: string
  port: number | null
  base_path: string
  enabled: boolean
  is_system: boolean
  rule_count: number
  has_credential: boolean
  has_agent_token: boolean
  agent_connected: boolean
  agent_last_seen_at: string | null
  search_indexing_enabled: boolean
}

export interface Rule {
  id: number
  order: number
  type: RuleType
  pattern: string
  pattern_kind: PatternKind
  notes: string | null
}

export interface SeverityPattern {
  id: number
  source_id: number | null
  level: SeverityLevel
  pattern: string
  pattern_kind: PatternKind
  enabled: boolean
  highlight_line: boolean
  include_in_navigation: boolean
}

export interface Role {
  id: number
  name: string
  is_builtin: boolean
  is_super_admin: boolean
  global_capabilities: GlobalCapability[]
}

export interface RoleGrant {
  id: number
  role_id: number
  scope_type: ScopeType
  scope_id: number
  capabilities: Capability[]
}

export interface AppUser {
  id: number
  username: string
  role_id: number
  active: boolean
  must_change_password: boolean
}

export interface BrowseEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
  is_archive: boolean
}

export interface ConnectionCheckResult {
  ok: boolean
  detail: string
}

export interface AgentTokenResult {
  token: string
}

export interface SSOProvider {
  id: number
  protocol: 'oidc'
  name: string
  enabled: boolean
  issuer: string
  client_id: string
  scopes: string
  group_claim: string | null
}

export interface SSOStatus {
  enabled: boolean
  name: string | null
}

export interface GroupRoleMapping {
  id: number
  order: number
  group_name: string
  role_id: number
  role_name: string
}

export type SearchMatchedField = 'content' | 'path'

export interface SearchHit {
  source_id: number
  file_path: string
  line_number: number
  snippet_html: string
  matched_field: SearchMatchedField
}

export interface SystemSettings {
  search_view_enabled: boolean
}

export interface Alert {
  id: number
  name: string
  query: string
  source_id: number | null
  webhook_url: string
  enabled: boolean
  last_checked_at: string | null
}

export interface AlertCreate {
  name: string
  query: string
  source_id?: number | null
  webhook_url: string
  enabled?: boolean
}

export interface AlertUpdate {
  name?: string
  query?: string
  source_id?: number | null
  webhook_url?: string
  enabled?: boolean
}

export interface MonitoringTokenResult {
  token: string
}

export interface MonitoringTokenStatus {
  configured: boolean
}
