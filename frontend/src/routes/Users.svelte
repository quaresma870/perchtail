<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
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

  const roleName = (id: number) => roles.find((r) => r.id === id)?.name ?? `#${id}`

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

<div class="page">
  <h1>Users</h1>

  {#if loading}
    <p>Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

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
            <td>{user.username}</td>
            <td>
              <select value={user.role_id} on:change={(e) => changeRole(user, Number((e.target as HTMLSelectElement).value))}>
                {#each roles as role (role.id)}
                  <option value={role.id}>{role.name}</option>
                {/each}
              </select>
            </td>
            <td>
              {user.active ? 'active' : 'inactive'}
              {#if user.must_change_password}<span class="badge">must change password</span>{/if}
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

    <form class="add-user" on:submit|preventDefault={createUser}>
      <h2>New user</h2>
      <input placeholder="username or email" bind:value={newUsername} required />
      <input type="password" placeholder="temporary password" bind:value={newPassword} required />
      <select bind:value={newRoleId}>
        {#each roles as role (role.id)}
          <option value={role.id}>{role.name}</option>
        {/each}
      </select>
      <button type="submit" disabled={creating}>{creating ? 'Creating…' : 'Create user'}</button>
    </form>
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  h1 {
    font-size: 1.3rem;
    margin: 0;
  }
  h2 {
    font-size: 1rem;
    margin: 0;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border-radius: 6px;
  }
  th,
  td {
    text-align: left;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid #eee;
    font-size: 0.88rem;
  }
  th {
    background: #f0f1f4;
  }
  .badge {
    margin-left: 0.4rem;
    font-size: 0.7rem;
    background: #e08b00;
    color: #fff;
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
  }
  button.link {
    border: none;
    background: none;
    color: #2f6fed;
    cursor: pointer;
    margin-right: 0.6rem;
    font-size: 0.85rem;
    padding: 0;
  }
  .temp-password {
    background: #fff8e1;
    font-size: 0.85rem;
  }
  .add-user {
    background: #fff;
    padding: 1rem 1.25rem;
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    max-width: 360px;
  }
  input,
  select {
    padding: 0.45rem;
    border: 1px solid #ccc;
    border-radius: 4px;
  }
  button[type='submit'] {
    align-self: flex-start;
    padding: 0.5rem 1.1rem;
    border: none;
    border-radius: 4px;
    background: #2f6fed;
    color: #fff;
    font-weight: 600;
  }
  .error {
    color: #c0392b;
  }
</style>
