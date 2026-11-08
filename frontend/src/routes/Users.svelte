<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import Modal from '../lib/components/Modal.svelte'
  import PasswordRules from '../lib/components/PasswordRules.svelte'
  import { confirmAction } from '../lib/confirm'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import SortableHeader from '../lib/components/SortableHeader.svelte'
  import {
    loadViewState,
    matchesText,
    nextSort,
    saveViewState,
    sortRows,
    type SortState,
  } from '../lib/table-view'
  import type { AppUser, Role } from '../lib/types'

  let users: AppUser[] = []
  let roles: Role[] = []
  let loading = true
  let error = ''

  let newUsername = ''
  let newPassword = ''
  let newDisplayName = ''
  let newEmail = ''
  let newRoleId: number | null = null
  let creating = false
  // Create and reset password open in popups (#301).
  let newUserOpen = false
  let createError = ''
  let resetResult: { username: string; password: string } | null = null
  // Edit (#356): name, email, role and status in one popup.
  type EditForm = { user: AppUser; displayName: string; email: string; roleId: number; active: boolean }
  let editing: EditForm | null = null
  let savingEdit = false
  let editError = ''
  let resetCopied = false

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

  function openNewUser() {
    newUsername = ''
    newPassword = ''
    newDisplayName = ''
    newEmail = ''
    if (roles.length > 0 && !roles.some((r) => r.id === newRoleId)) newRoleId = roles[0].id
    createError = ''
    newUserOpen = true
  }

  async function createUser() {
    if (!newUsername || !newPassword || newRoleId === null) return
    creating = true
    createError = ''
    try {
      await api.post('/users', {
        username: newUsername,
        password: newPassword,
        role_id: newRoleId,
        display_name: newDisplayName,
        email: newEmail,
      })
      newUserOpen = false
      newPassword = ''
      await load()
    } catch (err) {
      createError = err instanceof ApiError ? err.detail : 'Failed to create user'
    } finally {
      creating = false
    }
  }

  function openEdit(user: AppUser) {
    editError = ''
    editing = {
      user,
      displayName: user.display_name ?? '',
      email: user.email ?? '',
      roleId: user.role_id,
      active: user.active,
    }
  }

  async function saveEdit() {
    if (!editing) return
    const { user } = editing
    const patch: Record<string, unknown> = {}
    if (user.auth_provider === 'local') {
      if (editing.displayName.trim() !== (user.display_name ?? '')) patch.display_name = editing.displayName.trim()
      if (editing.email.trim() !== (user.email ?? '')) patch.email = editing.email.trim()
    }
    if (editing.roleId !== user.role_id) patch.role_id = editing.roleId
    if (editing.active !== user.active) {
      if (!editing.active) {
        const ok = await confirmAction({
          title: 'Deactivate user',
          message: `Deactivate ${user.username}? They're signed out and can't sign in until reactivated; their history is kept.`,
          confirmLabel: 'Deactivate',
          danger: true,
        })
        if (!ok) return
      }
      patch.active = editing.active
    }
    if (Object.keys(patch).length === 0) {
      editing = null
      return
    }
    savingEdit = true
    editError = ''
    try {
      await api.patch(`/users/${user.id}`, patch)
      editing = null
      await load()
    } catch (err) {
      editError = err instanceof ApiError ? err.detail : 'Failed to save the user'
    } finally {
      savingEdit = false
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
    if (
      user.active &&
      !(await confirmAction({
        title: 'Deactivate user',
        message: `Deactivate ${user.username}? They're signed out and can't sign in until reactivated; their history is kept.`,
        confirmLabel: 'Deactivate',
        danger: true,
      }))
    )
      return
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

  async function copyResetPassword() {
    if (!resetResult) return
    try {
      await navigator.clipboard.writeText(resetResult.password)
      resetCopied = true
    } catch {
      resetCopied = false
    }
  }

  async function resetPassword(user: AppUser) {
    const ok = await confirmAction({
      title: 'Reset password',
      message: `Reset the password of ${user.username}? They get a temporary password and must choose a new one when they sign in.`,
      confirmLabel: 'Reset password',
    })
    if (!ok) return
    try {
      const result = await api.post<{ temporary_password: string }>(
        `/users/${user.id}/reset-password`,
      )
      resetCopied = false
      resetResult = { username: user.username, password: result.temporary_password }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to reset password'
    }
  }

  // Sort and filters (#238), kept for the browser session.
  const VIEW_KEY = 'perchtail.view.users'
  let view = loadViewState(VIEW_KEY, {
    sort: null as SortState | null,
    text: '',
    roleId: '',
    status: '',
    provider: '',
  })
  $: saveViewState(VIEW_KEY, view)

  const roleName = (id: number) => roles.find((r) => r.id === id)?.name ?? `#${id}`
  const signInLabel = (u: AppUser) => (u.auth_provider === 'local' ? 'Local' : 'SSO')
  const formatLogin = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : 'never')
  const accessors: Record<string, (u: AppUser) => unknown> = {
    username: (u) => u.username,
    role: (u) => roleName(u.role_id),
    status: (u) => (u.active ? 0 : 1),
    provider: signInLabel,
    lastLogin: (u) => (u.last_login_at ? Date.parse(u.last_login_at) : null),
  }

  $: filtered = users.filter(
    (u) =>
      matchesText([u.username, u.display_name ?? '', u.email ?? '', roleName(u.role_id)], view.text) &&
      (!view.roleId || u.role_id === Number(view.roleId)) &&
      (!view.status || (view.status === 'active') === u.active) &&
      (!view.provider || (view.provider === 'local') === (u.auth_provider === 'local')),
  )
  $: visible = sortRows(filtered, view.sort, accessors)
  $: filtering = Boolean(view.text || view.roleId || view.status || view.provider)

  const sortBy = (key: string) => (view = { ...view, sort: nextSort(view.sort, key) })
  const clearFilters = () => (view = { ...view, text: '', roleId: '', status: '', provider: '' })

  onMount(load)
</script>

<SettingsNav />

<div class="page">
  <div class="page-head">
    <h1>Users</h1>
    {#if !loading}
      <button class="btn btn-primary" on:click={openNewUser}>+ New user</button>
    {/if}
  </div>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="filters">
      <input
        class="input filter-text"
        type="search"
        placeholder="Filter by username, name, email or role"
        aria-label="Filter users"
        bind:value={view.text}
      />
      <select class="input" aria-label="Role" bind:value={view.roleId}>
        <option value="">All roles</option>
        {#each roles as role (role.id)}
          <option value={String(role.id)}>{role.name}</option>
        {/each}
      </select>
      <select class="input" aria-label="Status" bind:value={view.status}>
        <option value="">Active and inactive</option>
        <option value="active">Active</option>
        <option value="inactive">Inactive</option>
      </select>
      <select class="input" aria-label="Sign-in" bind:value={view.provider}>
        <option value="">Local and SSO</option>
        <option value="local">Local</option>
        <option value="sso">SSO</option>
      </select>
      {#if filtering}
        <span class="filter-count">{visible.length} of {users.length}</span>
        <button class="link" on:click={clearFilters}>Clear filters</button>
      {/if}
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <SortableHeader label="Username" key="username" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Role" key="role" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Status" key="status" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Sign-in" key="provider" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Last login" key="lastLogin" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each visible as user (user.id)}
            <tr>
              <td class="username">
                <button class="link username-link" title="Edit {user.username}" on:click={() => openEdit(user)}
                  >{user.username}</button
                >
                {#if user.display_name || user.email}
                  <div class="user-sub">
                    {[user.display_name, user.email].filter(Boolean).join(' · ')}
                  </div>
                {/if}
              </td>
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
              <td>{signInLabel(user)}</td>
              <td class="last-login">{formatLogin(user.last_login_at)}</td>
              <td>
                <div class="row-actions">
                  <button class="btn btn-ghost btn-sm" on:click={() => openEdit(user)}>Edit</button>
                  {#if user.auth_provider === 'local'}
                    <button class="btn btn-ghost btn-sm" on:click={() => resetPassword(user)}>Reset password</button>
                  {/if}
                  {#if user.active}
                    <button class="btn btn-danger btn-sm" on:click={() => toggleActive(user)}>Deactivate</button>
                  {:else}
                    <button class="btn btn-ghost btn-sm" on:click={() => toggleActive(user)}>Reactivate</button>
                  {/if}
                </div>
              </td>
            </tr>
          {/each}
          {#if users.length > 0 && visible.length === 0}
            <tr>
              <td colspan="6" class="empty">
                No users match. <button class="link" on:click={clearFilters}>Clear filters</button>
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>

    {#if newUserOpen}
      <Modal title="New user" on:close={() => (newUserOpen = false)}>
        <form id="new-user-form" class="modal-form" on:submit|preventDefault={createUser}>
          <label class="field">Username <input class="input" bind:value={newUsername} required /></label>
          <label class="field">Display name (optional) <input class="input" bind:value={newDisplayName} /></label>
          <label class="field">Email (optional) <input class="input" type="email" bind:value={newEmail} /></label>
          <label class="field"
            >Temporary password
            <input class="input" type="password" autocomplete="new-password" bind:value={newPassword} required />
          </label>
          <PasswordRules />
          <label class="field"
            >Role
            <select class="input" bind:value={newRoleId}>
              {#each roles as role (role.id)}
                <option value={role.id}>{role.name}</option>
              {/each}
            </select>
          </label>
          {#if createError}
            <p class="error" role="alert">{createError}</p>
          {/if}
        </form>
        <svelte:fragment slot="footer">
          <button class="btn btn-ghost" type="button" on:click={() => (newUserOpen = false)}>Cancel</button>
          <button class="btn btn-primary" type="submit" form="new-user-form" disabled={creating}>
            {creating ? 'Creating…' : 'Create user'}
          </button>
        </svelte:fragment>
      </Modal>
    {/if}

    {#if editing}
      <Modal title="Edit {editing.user.username}" on:close={() => (editing = null)}>
        <form id="edit-user-form" class="modal-form" on:submit|preventDefault={saveEdit}>
          <label class="field">Username <input class="input" value={editing.user.username} disabled /></label>
          {#if editing.user.auth_provider === 'local'}
            <label class="field">Display name <input class="input" bind:value={editing.displayName} /></label>
            <label class="field">Email <input class="input" type="email" bind:value={editing.email} /></label>
          {:else}
            <label class="field">Display name <input class="input" value={editing.displayName} disabled /></label>
            <label class="field">Email <input class="input" value={editing.email} disabled /></label>
            <p class="hint">An SSO account's name and email come from the identity provider at each sign-in.</p>
          {/if}
          <label class="field"
            >Role
            <select class="input" bind:value={editing.roleId}>
              {#each roles as role (role.id)}
                <option value={role.id}>{role.name}</option>
              {/each}
            </select>
          </label>
          <label class="check-field">
            <input type="checkbox" bind:checked={editing.active} />
            Active (an inactive user can't sign in; their history is kept)
          </label>
          {#if editError}
            <p class="error" role="alert">{editError}</p>
          {/if}
        </form>
        <svelte:fragment slot="footer">
          <button class="btn btn-ghost" type="button" on:click={() => (editing = null)}>Cancel</button>
          <button class="btn btn-primary" type="submit" form="edit-user-form" disabled={savingEdit}>
            {savingEdit ? 'Saving…' : 'Save'}
          </button>
        </svelte:fragment>
      </Modal>
    {/if}

    {#if resetResult}
      <Modal title="Temporary password" on:close={() => (resetResult = null)}>
        <p class="modal-text">
          The new temporary password of <strong>{resetResult.username}</strong>, shown only now. Give it to them
          safely; they'll choose a new one when they sign in.
        </p>
        <code class="temp-password">{resetResult.password}</code>
        <svelte:fragment slot="footer">
          <button class="btn btn-ghost" type="button" on:click={copyResetPassword}
            >{resetCopied ? 'Copied' : 'Copy'}</button
          >
          <button class="btn btn-primary" type="button" on:click={() => (resetResult = null)}>Done</button>
        </svelte:fragment>
      </Modal>
    {/if}
  {/if}
</div>

<style>
  .username-link {
    font-weight: inherit;
    color: var(--text);
    padding: 0;
    text-align: left;
  }
  .username-link:hover {
    color: var(--accent);
    text-decoration: underline;
  }
  .check-field {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
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
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
  }
  .filter-text {
    flex: 1 1 260px;
    max-width: 420px;
  }
  .filter-count {
    color: var(--text-muted);
    font-size: 0.82rem;
  }
  .last-login {
    color: var(--text-muted);
    white-space: nowrap;
  }
  .empty {
    color: var(--text-muted);
    text-align: center;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  table :global(th),
  td {
    text-align: left;
    padding: 0.65rem 1rem;
    font-size: 0.88rem;
  }
  table :global(th) {
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
  .user-sub {
    font-size: 0.78rem;
    color: var(--text-faint);
    margin-top: 0.15rem;
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
    font-size: 0.85rem;
    padding: 0;
  }
  .page-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  .modal-form {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
  }
  .modal-text {
    margin: 0;
    font-size: 0.86rem;
    color: var(--text-muted);
    line-height: 1.5;
  }
  .temp-password {
    padding: 0.6rem 0.8rem;
    border-radius: var(--radius-sm);
    background: var(--warning-soft);
    color: var(--text);
    font-size: 0.95rem;
    user-select: all;
  }
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
