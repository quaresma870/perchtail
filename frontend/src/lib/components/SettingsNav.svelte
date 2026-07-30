<script lang="ts">
  import { currentUser, hasCapability } from '../auth'
  import { currentHash } from '../hash'

  const isActive = (prefix: string) => $currentHash === prefix || $currentHash.startsWith(prefix + '/')
</script>

<nav class="settings-nav">
  <a href="#/settings/sources" class:active={isActive('/settings/sources')}>Sources</a>
  {#if hasCapability($currentUser, 'manage_roles')}
    <a href="#/settings/roles" class:active={isActive('/settings/roles')}>Roles</a>
  {/if}
  {#if hasCapability($currentUser, 'manage_users')}
    <a href="#/settings/users" class:active={isActive('/settings/users')}>Users</a>
  {/if}
  {#if hasCapability($currentUser, 'manage_sso')}
    <a href="#/settings/sso" class:active={isActive('/settings/sso')}>SSO</a>
  {/if}
</nav>

<style>
  .settings-nav {
    display: flex;
    gap: 1.25rem;
    padding: 0 2rem;
    border-bottom: 1px solid var(--border-soft);
    background: var(--bg-elevated);
  }
  .settings-nav a {
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.85rem;
    font-weight: 500;
    padding: 0.65rem 0;
    border-bottom: 2px solid transparent;
  }
  .settings-nav a:hover {
    color: var(--text);
  }
  .settings-nav a.active {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
</style>
