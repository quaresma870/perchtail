<script lang="ts">
  import Router, { push } from 'svelte-spa-router'
  import { onMount } from 'svelte'
  import { authChecked, currentUser, logout, refreshCurrentUser, hasCapability } from './lib/auth'
  import { currentHash } from './lib/hash'
  import Login from './routes/Login.svelte'
  import ChangePassword from './routes/ChangePassword.svelte'
  import Sources from './routes/Sources.svelte'
  import SourceEditor from './routes/SourceEditor.svelte'
  import Viewer from './routes/Viewer.svelte'
  import Search from './routes/Search.svelte'
  import Roles from './routes/Roles.svelte'
  import RoleEditor from './routes/RoleEditor.svelte'
  import Users from './routes/Users.svelte'
  import SsoSettings from './routes/SsoSettings.svelte'

  const routes = {
    '/login': Login,
    '/change-password': ChangePassword,
    '/sources': Sources,
    '/sources/new': SourceEditor,
    '/sources/:id': SourceEditor,
    '/viewer': Viewer,
    '/viewer/:sourceId': Viewer,
    '/search': Search,
    '/roles': Roles,
    '/roles/new': RoleEditor,
    '/roles/:id': RoleEditor,
    '/users': Users,
    '/sso': SsoSettings,
  }

  onMount(async () => {
    await refreshCurrentUser()
  })

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

  const isActive = (prefix: string) => $currentHash === prefix || $currentHash.startsWith(prefix + '/')

  async function handleLogout() {
    await logout()
    push('/login')
  }
</script>

<main>
  {#if $authChecked && $currentUser && $currentHash !== '/login'}
    <nav>
      <div class="brand">
        <img src="/favicon.svg" alt="" width="26" height="26" />
        <span>PerchTail</span>
      </div>
      <a href="#/viewer" class:active={isActive('/viewer')}>Viewer</a>
      <a href="#/search" class:active={isActive('/search')}>Search</a>
      <a href="#/sources" class:active={isActive('/sources')}>Sources</a>
      {#if hasCapability($currentUser, 'manage_roles')}
        <a href="#/roles" class:active={isActive('/roles')}>Roles</a>
      {/if}
      {#if hasCapability($currentUser, 'manage_users')}
        <a href="#/users" class:active={isActive('/users')}>Users</a>
      {/if}
      {#if hasCapability($currentUser, 'manage_sso')}
        <a href="#/sso" class:active={isActive('/sso')}>SSO</a>
      {/if}
      <span class="spacer"></span>
      <span class="username">{$currentUser.username}</span>
      <button class="btn btn-ghost" on:click={handleLogout}>Log out</button>
    </nav>
  {/if}

  {#if $authChecked}
    <div class="content">
      <Router {routes} />
    </div>
  {/if}
</main>

<style>
  main {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
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
  .username {
    font-size: 0.82rem;
    color: var(--text-faint);
  }
  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
</style>
