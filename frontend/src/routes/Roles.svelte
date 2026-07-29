<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
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
      push(`/roles/${copy.id}`)
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

<div class="page">
  <div class="header">
    <h1>Roles</h1>
    <button on:click={() => push('/roles/new')}>New role</button>
  </div>

  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
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
              {role.name}
              {#if role.is_super_admin}<span class="badge">super-admin</span>{/if}
              {#if role.is_builtin}<span class="badge muted">built-in</span>{/if}
            </td>
            <td>{role.global_capabilities.join(', ') || '—'}</td>
            <td class="actions">
              <button class="link" on:click={() => push(`/roles/${role.id}`)}>edit</button>
              <button class="link" on:click={() => duplicate(role)}>duplicate</button>
              {#if !role.is_builtin}
                <button class="link danger" on:click={() => remove(role)}>delete</button>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }
  h1 {
    font-size: 1.3rem;
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
    padding: 0.55rem 0.75rem;
    border-bottom: 1px solid #eee;
    font-size: 0.9rem;
  }
  th {
    background: #f0f1f4;
  }
  .badge {
    margin-left: 0.4rem;
    font-size: 0.7rem;
    background: #2f6fed;
    color: #fff;
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
  }
  .badge.muted {
    background: #999;
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
  button.link.danger {
    color: #c0392b;
  }
  .error {
    color: #c0392b;
  }
</style>
