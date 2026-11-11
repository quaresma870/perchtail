<script lang="ts">
  import Router, { push } from 'svelte-spa-router'
  import { onMount } from 'svelte'
  import { authChecked, currentUser, hasCapability, logout, refreshCurrentUser } from './lib/auth'
  import { currentHash } from './lib/hash'
  import { serverReachable } from './lib/server-status'
  import { closeAllTabs } from './lib/viewer-tabs'
  import { refreshSystemSettings, refreshSystemSettingsIfStale, systemSettings } from './lib/settings'
  import Login from './routes/Login.svelte'
  import ChangePassword from './routes/ChangePassword.svelte'
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
  import SystemSettings from './routes/SystemSettings.svelte'
  import SeverityIndicatorsSettings from './routes/SeverityIndicatorsSettings.svelte'
  import Profile from './routes/Profile.svelte'
  import UserMenu from './lib/components/UserMenu.svelte'
  import ConfirmHost from './lib/components/ConfirmHost.svelte'
  import AuditLogSettings from './routes/AuditLogSettings.svelte'
  import AgentsSettings from './routes/AgentsSettings.svelte'

  const routes = {
    '/login': Login,
    '/change-password': ChangePassword,
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
    '/settings/system': SystemSettings,
    '/settings/severity-indicators': SeverityIndicatorsSettings,
    '/settings/audit-log': AuditLogSettings,
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

  $: if ($authChecked && !$currentUser && $currentHash !== '/login') {
    push('/login')
  }
  $: if (
    $authChecked &&
    $currentUser?.must_change_password &&
    $currentHash !== '/change-password'
  ) {
    push('/change-password')
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
  // The audit log can be turned off deployment-wide (Settings -> System);
  // guard the route itself, not just the nav link, so it's actually off for
  // a bookmarked/typed URL too. Also gated by view_audit_log (see
  // auth/models.py's GlobalCapability).
  $: if (
    $authChecked &&
    $currentUser &&
    (!$systemSettings.audit_view_enabled || !hasCapability($currentUser, 'view_audit_log')) &&
    $currentHash.startsWith('/settings/audit-log')
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
  {#if $authChecked && $currentUser && $currentHash !== '/login'}
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
