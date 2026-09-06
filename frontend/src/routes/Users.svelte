<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type { AppUser, Role } from '../lib/types'

  let users: AppUser[] = []
  let roles: Role[] = []
  let loading = true
  let error = ''

  let newUsername = ''
  let newPassword = ''
  let newRoleId: number | null = null
  let creating = false

  let tempPasswordFor: number | null = null
  let tempPassword = ''

  async function load() {
    loading = true
    error = ''
    try {
      ;[users, roles] = await Promise.all([
        api.get<AppUser[]>('/users'),
        api.get<Role[]>('/roles'),
      ])
      if (newRoleId === null && roles.length > 0) newRoleId = roles[0].id
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load users'
    } finally {
      loading = false
    }
  }

  async function createUser() {
    if (!newUsername || !newPassword || newRoleId === null) return
    creating = true
    error = ''
    try {
      await api.post('/users', { username: newUsername, password: newPassword, role_id: newRoleId })
      newUsername = ''
      newPassword = ''
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to create user'
    } finally {
      creating = false
    }
  }

  async function changeRole(user: AppUser, roleId: number) {
    try {
      await api.patch(`/users/${user.id}`, { role_id: roleId })
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update role'
    }
  }

  async function toggleActive(user: AppUser) {
    try {
      if (user.active) {
        await api.delete(`/users/${user.id}`)
      } else {
        await api.patch(`/users/${user.id}`, { active: true })
      }
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update user'
    }
  }

  async function resetPassword(user: AppUser) {
    try {
      const result = await api.post<{ temporary_password: string }>(
        `/users/${user.id}/reset-password`,
      )
      tempPasswordFor = user.id
      tempPassword = result.temporary_password
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to reset password'
    }
  }

  onMount(load)
</script>

<SettingsNav />

<div class="page">
  <h1>Users</h1>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Username</th>
            <th>Role</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each users as user (user.id)}
            <tr>
              <td class="username">{user.username}</td>
              <td>
                <select
                  class="input"
                  value={user.role_id}
                  on:change={(e) =>
                    changeRole(user, Number((e.target as HTMLSelectElement).value))}
                >
                  {#each roles as role (role.id)}
                    <option value={role.id}>{role.name}</option>
                  {/each}
                </select>
              </td>
              <td>
                <span class="badge" class:badge-ok={user.active} class:badge-muted={!user.active}>
                  {user.active ? 'active' : 'inactive'}
                </span>
                {#if user.must_change_password}
                  <span class="badge badge-warn">must change password</span>
                {/if}
              </td>
              <td class="actions">
                <button class="link" on:click={() => resetPassword(user)}>reset password</button>
                <button class="link" on:click={() => toggleActive(user)}>
                  {user.active ? 'deactivate' : 'reactivate'}
                </button>
              </td>
            </tr>
            {#if tempPasswordFor === user.id}
              <tr>
                <td colspan="4" class="temp-password">
                  Temporary password (shown once): <code>{tempPassword}</code>
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>

    <form class="add-user card" on:submit|preventDefault={createUser}>
      <h2>New user</h2>
      <input class="input" placeholder="username or email" bind:value={newUsername} required />
      <input
        class="input"
        type="password"
        placeholder="temporary password"
        bind:value={newPassword}
        required
      />
      <select class="input" bind:value={newRoleId}>
        {#each roles as role (role.id)}
          <option value={role.id}>{role.name}</option>
        {/each}
      </select>
      <button class="btn btn-primary" type="submit" disabled={creating}>
        {creating ? 'Creating…' : 'Create user'}
      </button>
    </form>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  h2 {
    font-size: 1rem;
    margin: 0;
    color: var(--text);
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 0.65rem 1rem;
    font-size: 0.88rem;
  }
  th {
    color: var(--text-faint);
    font-weight: 600;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border);
  }
  tbody tr {
    border-bottom: 1px solid var(--border-soft);
  }
  tbody tr:last-child {
    border-bottom: none;
  }
  .username {
    font-weight: 600;
    color: var(--text);
  }
  .badge-ok {
    background: var(--success-soft);
    color: var(--success);
  }
  .badge-muted {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .badge-warn {
    background: var(--warning-soft);
    color: var(--warning);
    margin-left: 0.4rem;
  }
  button.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    margin-right: 0.6rem;
    font-size: 0.85rem;
    padding: 0;
  }
  .temp-password {
    background: var(--warning-soft);
    color: var(--text);
    font-size: 0.85rem;
  }
  .add-user {
    padding: 1.25rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    max-width: 360px;
  }
  button[type='submit'] {
    align-self: flex-start;
    padding: 0.6rem 1.2rem;
  }
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
