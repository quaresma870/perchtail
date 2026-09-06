<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type { Role } from '../lib/types'

  let roles: Role[] = []
  let loading = true
  let error = ''

  async function load() {
    loading = true
    error = ''
    try {
      roles = await api.get<Role[]>('/roles')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load roles'
    } finally {
      loading = false
    }
  }

  async function duplicate(role: Role) {
    try {
      const copy = await api.post<Role>(`/roles/${role.id}/duplicate`, {})
      push(`/settings/roles/${copy.id}`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to duplicate role'
    }
  }

  async function remove(role: Role) {
    if (!confirm(`Delete role "${role.name}"?`)) return
    try {
      await api.delete(`/roles/${role.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete role'
    }
  }

  onMount(load)
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Roles</h1>
    <button class="btn btn-primary" on:click={() => push('/settings/roles/new')}>+ New role</button>
  </div>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Global capabilities</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each roles as role (role.id)}
            <tr>
              <td>
                <span class="role-name">{role.name}</span>
                {#if role.is_super_admin}<span class="badge badge-accent">super-admin</span>{/if}
                {#if role.is_builtin}<span class="badge badge-muted">built-in</span>{/if}
              </td>
              <td class="caps">{role.global_capabilities.join(', ') || '—'}</td>
              <td class="actions">
                <button class="link" on:click={() => push(`/settings/roles/${role.id}`)}
                  >edit</button
                >
                <button class="link" on:click={() => duplicate(role)}>duplicate</button>
                {#if !role.is_builtin}
                  <button class="link danger" on:click={() => remove(role)}>delete</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
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
    padding: 0.75rem 1rem;
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
  tbody tr:hover {
    background: var(--bg-hover);
  }
  .role-name {
    font-weight: 600;
    color: var(--text);
    margin-right: 0.4rem;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .badge-muted {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .caps {
    color: var(--text-muted);
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
  button.link.danger {
    color: var(--danger);
  }
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
