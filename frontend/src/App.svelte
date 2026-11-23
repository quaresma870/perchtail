<script lang="ts">
  import Router, { push, replace } from 'svelte-spa-router'
  import { onMount } from 'svelte'
  import { authChecked, currentUser, logout, mfaSetupRequired, refreshCurrentUser } from './lib/auth'
  import { canOpenAuditLogPage } from './lib/settings-nav'
  import { currentHash } from './lib/hash'
  import { serverReachable } from './lib/server-status'
  import { closeAllTabs } from './lib/viewer-tabs'
  import { refreshSystemSettings, refreshSystemSettingsIfStale, systemSettings } from './lib/settings'
  import Login from './routes/Login.svelte'
  import ForgotPassword from './routes/ForgotPassword.svelte'
  import ResetPassword from './routes/ResetPassword.svelte'
  import ChangePassword from './routes/ChangePassword.svelte'
  import SetupMfa from './routes/SetupMfa.svelte'
  import Sources from './routes/Sources.svelte'
  import SourceEditorPopup from './routes/SourceEditorPopup.svelte'
  import SourceOverview from './routes/SourceOverview.svelte'
  import Folders from './routes/Folders.svelte'
  import Viewer from './routes/Viewer.svelte'
  import ImportExportSettings from './routes/ImportExportSettings.svelte'
  import Alerts from './routes/Alerts.svelte'
  import SettingsIndex from './routes/SettingsIndex.svelte'
  import Roles from './routes/Roles.svelte'
  import RoleEditorPopup from './routes/RoleEditorPopup.svelte'
  import Users from './routes/Users.svelte'
  import SsoSettings from './routes/SsoSettings.svelte'
  import EmailSettings from './routes/EmailSettings.svelte'
  import SourceDefaultsSettings from './routes/SourceDefaultsSettings.svelte'
  import SignInSecuritySettings from './routes/SignInSecuritySettings.svelte'
  import ExplorerSettings from './routes/ExplorerSettings.svelte'
  import AlertsSettings from './routes/AlertsSettings.svelte'
  import BackupsSettings from './routes/BackupsSettings.svelte'
  import MonitoringSettings from './routes/MonitoringSettings.svelte'
  import SeverityIndicatorsSettings from './routes/SeverityIndicatorsSettings.svelte'
  import RedactionSettings from './routes/RedactionSettings.svelte'
  import Profile from './routes/Profile.svelte'
  import UserMenu from './lib/components/UserMenu.svelte'
  import ConfirmHost from './lib/components/ConfirmHost.svelte'
  import AuditLogSettings from './routes/AuditLogSettings.svelte'
  import AgentsSettings from './routes/AgentsSettings.svelte'
  import AccessReport from './routes/AccessReport.svelte'

  const routes = {
    '/login': Login,
    '/forgot-password': ForgotPassword,
    '/reset-password': ResetPassword,
    '/reset-password/:token': ResetPassword,
    '/change-password': ChangePassword,
    '/setup-mfa': SetupMfa,
    '/profile': Profile,
    '/viewer': Viewer,
    '/viewer/:sourceId': Viewer,
    '/alerts': Alerts,
    '/settings': SettingsIndex,
    '/settings/sources': Sources,
    '/settings/sources/new': SourceEditorPopup,
    '/settings/sources/:id/edit': SourceEditorPopup,
    '/settings/sources/:id': SourceOverview,
    '/settings/folders': Folders,
    '/settings/agents': AgentsSettings,
    '/settings/roles': Roles,
    '/settings/roles/new': RoleEditorPopup,
    '/settings/roles/:id': RoleEditorPopup,
    '/settings/users': Users,
    '/settings/sso': SsoSettings,
    '/settings/email': EmailSettings,
    '/settings/source-defaults': SourceDefaultsSettings,
    '/settings/sign-in-security': SignInSecuritySettings,
    '/settings/explorer': ExplorerSettings,
    '/settings/alerts': AlertsSettings,
    '/settings/backups': BackupsSettings,
    '/settings/monitoring': MonitoringSettings,
    '/settings/severity-indicators': SeverityIndicatorsSettings,
    '/settings/redaction': RedactionSettings,
    '/settings/audit-log': AuditLogSettings,
    '/settings/access-review': AccessReport,
    '/settings/import-export': ImportExportSettings,
  }

  onMount(async () => {
    await refreshCurrentUser()
    await refreshSystemSettings()
  })

  // Re-read system settings on navigation and when the tab is shown again,
  // so a change an admin makes (e.g. the Connections view) applies without a
  // reload. Also covers the first navigation after logging in, when the
  // fetch on mount had no session yet.
  $: if ($currentUser && $currentHash) refreshSystemSettingsIfStale()
  function onVisibility() {
    if (document.visibilityState === 'visible' && $currentUser) refreshSystemSettingsIfStale()
  }

  // Pages for someone who isn't signed in: the login and password recovery
  // by email (#244). Every other page sends them to the login.
  const PUBLIC_PATHS = ['/login', '/forgot-password', '/reset-password']
  const isPublicPath = (path: string) =>
    PUBLIC_PATHS.some((prefix) => path === prefix || path.startsWith(prefix + '/'))
  $: if ($authChecked && !$currentUser && !isPublicPath($currentHash)) {
    push('/login')
  }
  $: if (
    $authChecked &&
    $currentUser?.must_change_password &&
    $currentHash !== '/change-password'
  ) {
    push('/change-password')
  }
  // After the password: an account that must use MFA sets it up before
  // anything else (the API refuses everything else until it does).
  $: if (
    $authChecked &&
    $currentUser &&
    !$currentUser.must_change_password &&
    mfaSetupRequired($currentUser) &&
    $currentHash !== '/setup-mfa'
  ) {
    push('/setup-mfa')
  }
  // The bare address has no page of its own: it opens the Viewer, as
  // signing in does.
  $: if ($authChecked && $currentUser && ($currentHash === '/' || $currentHash === '')) {
    push('/viewer')
  }
  // Settings -> Security and Sessions moved to My profile (#243); old links
  // still land there for a release.
  $: if ($currentHash === '/settings/security' || $currentHash === '/settings/sessions') {
    push('/profile')
  }
  // The System page was split into the pages of each Settings section; its
  // old address opens the first of them.
  $: if ($currentHash === '/settings/system') {
    replace('/settings/explorer')
  }
  // The audit log's viewer can be turned off deployment-wide (Settings ->
  // Audit log); guard the route itself, not just the nav link, so it's
  // actually off for a bookmarked/typed URL too. Also gated by view_audit_log
  // (see auth/models.py's GlobalCapability). Whoever manages system settings
  // keeps the page for its settings, where the viewer is turned on again.
  $: if (
    $authChecked &&
    $currentUser &&
    !canOpenAuditLogPage($currentUser, $systemSettings.audit_view_enabled) &&
    $currentHash.startsWith('/settings/audit-log')
  ) {
    push('/settings')
  }

  // Redaction (#382, #249) is configured by super admins only; the API
  // refuses anyone else too.
  $: if (
    $authChecked &&
    $currentUser &&
    !$currentUser.is_super_admin &&
    $currentHash.startsWith('/settings/redaction')
  ) {
    push('/settings')
  }

  const isActive = (prefix: string) => $currentHash === prefix || $currentHash.startsWith(prefix + '/')

  async function handleLogout() {
    await closeAllTabs()
    await logout()
    push('/login')
  }
</script>

<svelte:document on:visibilitychange={onVisibility} />

<main>
  {#if $authChecked && $currentUser && !isPublicPath($currentHash)}
    <nav>
      <div class="brand">
        <img src="/favicon.svg" alt="" width="26" height="26" />
        <span>PerchTail</span>
      </div>
      <a href="#/viewer" class:active={isActive('/viewer')}>Viewer</a>
      <a href="#/alerts" class:active={isActive('/alerts')}>Alerts</a>
      <a href="#/settings" class:active={isActive('/settings')}>Settings</a>
      <span class="spacer"></span>
      <UserMenu
        name={$currentUser.display_name ?? $currentUser.username}
        active={isActive('/profile')}
        on:logout={handleLogout}
      />
    </nav>
  {/if}

  {#if !$serverReachable}
    <div class="server-banner" role="alert">
      Can't reach PerchTail — retrying… Source statuses may be out of date until it answers.
    </div>
  {/if}

  {#if $authChecked}
    <div class="content">
      <Router {routes} />
    </div>
  {/if}
  <ConfirmHost />
</main>

<style>
  /* The window itself never scrolls (#154): the nav stays put, ordinary
     pages scroll inside .content, and the viewer fills .content and
     scrolls only its own panes. */
  main {
    display: flex;
    flex-direction: column;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
  }
  nav {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    padding: 0.65rem 1.5rem;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 700;
    font-size: 1.02rem;
    margin-right: 0.5rem;
    color: var(--text);
  }
  nav a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.88rem;
    font-weight: 500;
    padding: 0.3rem 0;
    border-bottom: 2px solid transparent;
  }
  nav a:hover {
    color: var(--text);
  }
  nav a.active {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
  .spacer {
    flex: 1;
  }
  .server-banner {
    padding: 0.45rem 1.5rem;
    background: var(--warning-soft);
    color: var(--warning);
    font-size: 0.85rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: auto;
  }
</style>
