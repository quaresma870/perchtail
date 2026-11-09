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
  | 'view_audit_log'
  | 'manage_alerts'

export interface CurrentUser {
  id: number
  username: string
  // My profile (#243); read-only for SSO accounts (auth_provider not 'local').
  display_name: string | null
  email: string | null
  auth_provider: 'local' | 'oidc' | 'saml'
  // The display choices this user made (#273); missing keys follow the
  // System defaults.
  preferences: Partial<Record<'explorer_sort' | 'connections_view', string>>
  role_id: number
  active: boolean
  must_change_password: boolean
  mfa_enabled: boolean
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

export interface FolderPathEntry {
  id: number
  name: string
}

export interface Source {
  id: number
  name: string
  customer_id: number | null
  customer_name: string | null
  folder_id: number | null
  folder_name: string | null
  // Full ancestor chain, root-to-leaf, excluding the customer itself --
  // lets the Viewer's connections-home tree reconstruct the whole nested
  // customer/folder hierarchy client-side without a separate (admin-gated)
  // /folders call. Empty when the source sits directly under its customer.
  folder_path: FolderPathEntry[]
  protocol: Protocol
  host: string
  port: number | null
  base_path: string
  // Two or more folders read by this source (#161), shown as top-level
  // folders named by label; empty for an ordinary single-folder source.
  roots: SourceRoot[]
  // WinRM (#169): the stored scheme (null = still inferred from the port)
  // and the one in effect; a CA certificate trusted for its HTTPS listener.
  winrm_scheme: 'https' | 'http' | null
  // JEA endpoint (#169), or null for the WinRS shell.
  winrm_configuration: string | null
  winrm_effective_scheme: 'https' | 'http' | null
  tls_ca_pem: string | null
  enabled: boolean
  is_system: boolean
  rule_count: number
  has_credential: boolean
  // When the credential was last set (#248); null if unknown or none.
  credential_updated_at: string | null
  has_agent_token: boolean
  agent_connected: boolean
  agent_last_seen_at: string | null
  agent_os: string | null
  // The version the agent reported (#215), and whether it's older than the server.
  agent_version: string | null
  agent_outdated: boolean
  // The latest upgrade from the portal (#319) since the server started.
  agent_upgrade: AgentUpgrade | null
  // Reachability (#276): last known, shared by every user; for an agent
  // source, its live connection.
  status: 'ok' | 'failing' | 'unknown'
  status_checked_at: string | null
  status_message: string | null
  // File-name index (#304): on/off, when it last listed the source, how many
  // files it found, and why the last pass stopped short (null: it didn't).
  name_index_enabled: boolean
  name_indexed_at: string | null
  name_index_files: number | null
  name_index_message: string | null
  // What the current user may do on this source (#174); the backend still
  // enforces every action on its own.
  my_capabilities: Capability[]
}

export interface SourceRoot {
  label: string
  path: string
}

export interface FolderPickEntry {
  name: string
  path: string
}

export interface FolderPickResult {
  protocol: Protocol
  path: string
  parent: string | null
  kind: 'folders' | 'shares' | 'drives'
  entries: FolderPickEntry[]
  truncated: boolean
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
  display_name: string | null
  email: string | null
  role_id: number
  active: boolean
  must_change_password: boolean
  auth_provider: 'local' | 'oidc' | 'saml'
  last_login_at: string | null
}

export interface BrowseEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
  is_archive: boolean
  // Last modification, seconds since the epoch (#273); null when the
  // source doesn't say (push agents older than 0.6).
  mtime?: number | null
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

export interface SystemSettings {
  audit_view_enabled: boolean
  audit_retention_days: number
  // Folders one source may read (#161): 1 = feature off, 100 at most.
  source_max_roots: number
  connections_view_switchable: boolean
  connections_view_default: 'list' | 'tree'
  // The daily release check (#215); UPDATE_CHECK=false overrides it.
  update_check_enabled: boolean
  // Password policy (#245).
  password_min_length: number
  password_max_length: number
  password_reject_simple: boolean
  password_reject_personal: boolean
  password_history: number
  password_required_classes: number
  password_max_age_days: number
  // Explorer (#272, #273).
  explorer_sort_default: ExplorerSort
  explorer_refresh_seconds: number
  tail_interval_seconds: number
  // Background source check (#276), minutes; 0 = off.
  source_check_minutes: number
  // File-name index (#304).
  name_index_enabled: boolean
  name_index_minutes: number
  // Live alerts (#305): seconds between checks, 15..3600.
  alerts_interval_seconds: number
  // Warn about credentials older than this (#248), days; 0 = off.
  credential_max_age_days: number
  // Server-side limits (#344): listings, opens, downloads and folder ZIPs
  // at once in all and per source; downloads and ZIPs per user.
  concurrency_global: number
  concurrency_per_source: number
  concurrency_user_transfers: number
  // Most entries a folder or archive listing returns (#347).
  listing_max_entries: number
  // Upgrading push agents from the portal (#319); off by default.
  agent_remote_upgrade_enabled: boolean
}

export type ExplorerSort = 'name_asc' | 'name_desc' | 'mtime_desc' | 'mtime_asc' | 'size_desc'

// GET /auth/password-policy (#245): the rules a new password must follow.
export interface PasswordPolicy {
  min_length: number
  max_length: number
  reject_simple: boolean
  reject_personal: boolean
  history: number
  required_classes: number
  max_age_days: number
}

// Settings -> System -> Updates (#215), super admins only.
export interface UpdateStatus {
  enabled: boolean
  disabled_by_env: boolean
  running_version: string
  install_method: 'docker' | 'source'
  latest_version: string | null
  latest_name: string | null
  latest_url: string | null
  latest_published_at: string | null
  update_available: boolean
  last_checked_at: string | null
  last_success_at: string | null
  last_error: string | null
}

export interface AuditLogEntry {
  id: number
  user_id: number | null
  username: string | null
  action: string
  target_type: string | null
  target_id: number | null
  timestamp: string
  metadata: Record<string, unknown> | null
  // #239: the target by name, as recorded, or its current name for entries
  // older than 0.5.3 (target_name_is_current).
  target_name: string | null
  target_context: string | null
  target_name_is_current: boolean
  target_exists: boolean
}

export interface AuditLogPage {
  items: AuditLogEntry[]
  total: number
}

export interface AuditLogFilterOptions {
  actions: string[]
  target_types: string[]
  /** Per named target type, the targets in the log (newest first). */
  targets: Record<string, { id: number; name: string }[]>
}

export interface AuditIntegrityStatus {
  // 'race_breaks': the only breaks match the #213 race and a super admin can
  // record them as known (#217).
  status: 'unknown' | 'ok' | 'broken' | 'race_breaks'
  last_checked_at: string | null
  broken_row_id: number | null
  race_break_count: number
  known_break_count: number
  known_breaks_recorded_at: string | null
}

export interface AuthSessionInfo {
  id: number
  created_at: string
  last_seen_at: string | null
  expires_at: string
  user_agent: string | null
  is_current: boolean
}

export interface MfaEnrollResponse {
  secret: string
  otpauth_uri: string
}

export interface MfaBackupCodesResponse {
  backup_codes: string[]
}

// Live alerts (#305): at most one of source_id/folder_id/customer_id is set;
// none means every source the owner can view.
export interface Alert {
  id: number
  name: string
  query: string
  regex: boolean
  source_id: number | null
  folder_id: number | null
  customer_id: number | null
  file_glob: string
  webhook_url: string
  enabled: boolean
  last_checked_at: string | null
  last_fired_at: string | null
  failed_deliveries: number
  last_error: string | null
}

export interface AlertCreate {
  name: string
  query: string
  regex?: boolean
  source_id?: number | null
  folder_id?: number | null
  customer_id?: number | null
  file_glob?: string
  webhook_url: string
  enabled?: boolean
}

// Sending any of the scope fields replaces the whole scope.
export type AlertUpdate = Partial<AlertCreate>

export interface AlertTestResult {
  ok: boolean
  error: string | null
}

export interface MonitoringTokenResult {
  token: string
}

export interface MonitoringTokenStatus {
  configured: boolean
}

// A push agent's upgrade from the portal (#319).
export interface AgentUpgrade {
  status: 'requested' | 'running' | 'restarting' | 'succeeded' | 'failed'
  // downloading, installing or restarting, as the agent reports it.
  stage: string | null
  detail: string | null
  from_version: string | null
  to_version: string
  started_at: string
  finished_at: string | null
}

// GET /agent-upgrades: whether upgrades from the portal are on, the version
// agents are upgraded to, and whether this server carries their binaries.
export interface AgentUpgradeAvailability {
  enabled: boolean
  version: string
  bundled: boolean
  bundled_reason: string | null
}

export type AgentUpgradeResult = 'accepted' | 'refused' | 'too_old' | 'failed' | 'busy'

export interface AgentUpgradeStarted {
  result: AgentUpgradeResult
  detail: string | null
  upgrade: AgentUpgrade | null
}

export interface BulkAgentUpgradeResult {
  source_id: number
  name: string | null
  result: AgentUpgradeResult | 'skipped'
  detail: string | null
}
