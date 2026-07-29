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
  import Roles from './routes/Roles.svelte'
  import RoleEditor from './routes/RoleEditor.svelte'
  import Users from './routes/Users.svelte'

  const routes = {
    '/login': Login,
    '/change-password': ChangePassword,
    '/sources': Sources,
    '/sources/new': SourceEditor,
    '/sources/:id': SourceEditor,
    '/viewer': Viewer,
    '/viewer/:sourceId': Viewer,
    '/roles': Roles,
    '/roles/new': RoleEditor,
    '/roles/:id': RoleEditor,
    '/users': Users,
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

  async function handleLogout() {
    await logout()
    push('/login')
  }
</script>

<main>
  {#if $authChecked && $currentUser && $currentHash !== '/login'}
    <nav>
      <div class="brand">PerchTail</div>
      <a href="#/viewer">Viewer</a>
      <a href="#/sources">Sources</a>
      {#if hasCapability($currentUser, 'manage_roles')}
        <a href="#/roles">Roles</a>
      {/if}
      {#if hasCapability($currentUser, 'manage_users')}
        <a href="#/users">Users</a>
      {/if}
      <span class="spacer"></span>
      <span class="username">{$currentUser.username}</span>
      <button on:click={handleLogout}>Log out</button>
    </nav>
  {/if}

  {#if $authChecked}
    <div class="content">
      <Router {routes} />
    </div>
  {/if}
</main>

<style>
  :global(body) {
    margin: 0;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: #f5f6f8;
    color: #1a1a1a;
  }
  main {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }
  nav {
    display: flex;
    align-items: center;
    gap: 1.25rem;
    padding: 0.6rem 1.25rem;
    background: #1f2430;
    color: #eee;
  }
  nav a {
    color: #cfd6e4;
    text-decoration: none;
    font-size: 0.9rem;
  }
  nav a:hover {
    color: #fff;
  }
  .brand {
    font-weight: 700;
    margin-right: 0.5rem;
  }
  .spacer {
    flex: 1;
  }
  .username {
    font-size: 0.85rem;
    color: #9aa4b8;
  }
  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  button {
    cursor: pointer;
  }
</style>
