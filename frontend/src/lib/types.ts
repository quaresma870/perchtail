export type Protocol = 'ssh' | 'smb' | 'winrm' | 'local' | 'agent'
export type RuleType = 'include' | 'exclude'
export type PatternKind = 'glob' | 'regex'
export type ScopeType = 'customer' | 'folder' | 'source'
export type Capability = 'view' | 'download' | 'manage_rules' | 'run_now'
export type GlobalCapability = 'manage_users' | 'manage_roles' | 'manage_sso' | 'create_source'

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
  folder_id: number | null
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
}

export interface Rule {
  id: number
  order: number
  type: RuleType
  pattern: string
  pattern_kind: PatternKind
  notes: string | null
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
}

export interface SSOStatus {
  enabled: boolean
  name: string | null
}
