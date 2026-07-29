<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import type { Customer, Folder, GlobalCapability, Role, RoleGrant, Source, Capability, ScopeType } from '../lib/types'

  export let params: { id?: string } = {}

  const isNew = !params.id
  const roleId = params.id ? Number(params.id) : null

  const ALL_GLOBAL_CAPS: GlobalCapability[] = [
    'manage_users',
    'manage_roles',
    'manage_sso',
    'create_source',
  ]
  const ALL_CAPS: Capability[] = ['view', 'download', 'manage_rules', 'run_now']

  let name = ''
  let isSuperAdmin = false
  let globalCaps = new Set<GlobalCapability>()
  let loading = !isNew
  let saving = false
  let error = ''

  let grants: RoleGrant[] = []
  let customers: Customer[] = []
  let folders: Folder[] = []
  let sources: Source[] = []

  let newScopeType: ScopeType = 'customer'
  let newScopeId: number | null = null
  let newCaps = new Set<Capability>()

  $: canEditSuperAdmin = $currentUser?.is_super_admin ?? false

  function toggle(set: Set<any>, value: any) {
    const next = new Set(set)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    return next
  }

  async function loadRole() {
    if (roleId === null) return
    const role = await api.get<Role>(`/roles/${roleId}`)
    name = role.name
    isSuperAdmin = role.is_super_admin
    globalCaps = new Set(role.global_capabilities)
    grants = await api.get<RoleGrant[]>(`/roles/${roleId}/grants`)
  }

  async function loadScopeOptions() {
    customers = await api.get<Customer[]>('/customers').catch(() => [])
    folders = await api.get<Folder[]>('/folders').catch(() => [])
    sources = await api.get<Source[]>('/sources').catch(() => [])
  }

  onMount(async () => {
    try {
      await Promise.all([loadRole(), loadScopeOptions()])
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load role'
    } finally {
      loading = false
    }
  })

  function scopeLabel(grant: RoleGrant): string {
    if (grant.scope_type === 'customer') {
      return customers.find((c) => c.id === grant.scope_id)?.name ?? `customer #${grant.scope_id}`
    }
    if (grant.scope_type === 'folder') {
      return folders.find((f) => f.id === grant.scope_id)?.name ?? `folder #${grant.scope_id}`
    }
    return sources.find((s) => s.id === grant.scope_id)?.name ?? `source #${grant.scope_id}`
  }

  function scopeOptions() {
    if (newScopeType === 'customer') return customers.map((c) => ({ id: c.id, label: c.name }))
    if (newScopeType === 'folder') return folders.map((f) => ({ id: f.id, label: f.name }))
    return sources.map((s) => ({ id: s.id, label: s.name }))
  }

  async function handleSubmit() {
    error = ''
    saving = true
    try {
      const payload = {
        name,
        is_super_admin: isSuperAdmin,
        global_capabilities: Array.from(globalCaps),
      }
      if (isNew) {
        const created = await api.post<Role>('/roles', payload)
        push(`/roles/${created.id}`)
      } else {
        await api.patch(`/roles/${roleId}`, payload)
        await loadRole()
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to save role'
    } finally {
      saving = false
    }
  }

  async function addGrant() {
    if (roleId === null || newScopeId === null || newCaps.size === 0) return
    try {
      await api.post(`/roles/${roleId}/grants`, {
        scope_type: newScopeType,
        scope_id: newScopeId,
        capabilities: Array.from(newCaps),
      })
      newScopeId = null
      newCaps = new Set()
      grants = await api.get<RoleGrant[]>(`/roles/${roleId}/grants`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to add grant'
    }
  }

  async function removeGrant(grant: RoleGrant) {
    if (roleId === null) return
    try {
      await api.delete(`/roles/${roleId}/grants/${grant.id}`)
      grants = grants.filter((g) => g.id !== grant.id)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to remove grant'
    }
  }
</script>

<div class="page">
  <div class="header">
    <h1>{isNew ? 'New role' : `Edit ${name}`}</h1>
    <button class="link" on:click={() => push('/roles')}>← back to roles</button>
  </div>

  {#if loading}
    <p>Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <form on:submit|preventDefault={handleSubmit}>
      <label>
        Name
        <input bind:value={name} required />
      </label>

      <label class="checkbox">
        <input type="checkbox" bind:checked={isSuperAdmin} disabled={!canEditSuperAdmin} />
        Super-admin (bypasses all grants, reaches system sources)
      </label>
      {#if !canEditSuperAdmin}
        <p class="hint">Only a super-admin can grant or revoke this flag.</p>
      {/if}

      <fieldset>
        <legend>Global capabilities</legend>
        {#each ALL_GLOBAL_CAPS as cap}
          <label class="checkbox">
            <input
              type="checkbox"
              checked={globalCaps.has(cap)}
              on:change={() => (globalCaps = toggle(globalCaps, cap))}
            />
            {cap}
          </label>
        {/each}
      </fieldset>

      <button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
    </form>

    {#if !isNew}
      <div class="grants">
        <h2>Access grants</h2>
        <p class="hint">
          Most specific scope wins: a source grant beats a folder grant, which beats a (possibly
          deeper) parent folder's, which beats the customer grant.
        </p>
        <table>
          <thead>
            <tr>
              <th>Scope</th>
              <th>Target</th>
              <th>Capabilities</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each grants as grant (grant.id)}
              <tr>
                <td>{grant.scope_type}</td>
                <td>{scopeLabel(grant)}</td>
                <td>{grant.capabilities.join(', ') || '—'}</td>
                <td>
                  <button class="link danger" on:click={() => removeGrant(grant)}>remove</button>
                </td>
              </tr>
            {/each}
            {#if grants.length === 0}
              <tr>
                <td colspan="4" class="empty">No grants yet — this role can't see anything.</td>
              </tr>
            {/if}
          </tbody>
        </table>

        <form class="add-grant" on:submit|preventDefault={addGrant}>
          <select bind:value={newScopeType} on:change={() => (newScopeId = null)}>
            <option value="customer">customer</option>
            <option value="folder">folder</option>
            <option value="source">source</option>
          </select>
          <select bind:value={newScopeId}>
            <option value={null}>— pick —</option>
            {#each scopeOptions() as opt (opt.id)}
              <option value={opt.id}>{opt.label}</option>
            {/each}
          </select>
          <div class="cap-checks">
            {#each ALL_CAPS as cap}
              <label class="checkbox small">
                <input
                  type="checkbox"
                  checked={newCaps.has(cap)}
                  on:change={() => (newCaps = toggle(newCaps, cap))}
                />
                {cap}
              </label>
            {/each}
          </div>
          <button type="submit">Add grant</button>
        </form>
      </div>
    {/if}
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
    max-width: 720px;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  h1 {
    font-size: 1.3rem;
    margin: 0;
  }
  h2 {
    font-size: 1rem;
    margin: 0 0 0.5rem;
  }
  button.link {
    border: none;
    background: none;
    color: #2f6fed;
    cursor: pointer;
    font-size: 0.85rem;
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    background: #fff;
    padding: 1.25rem;
    border-radius: 6px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    font-size: 0.85rem;
    color: #444;
  }
  label.checkbox {
    flex-direction: row;
    align-items: center;
    gap: 0.5rem;
  }
  label.checkbox.small {
    font-size: 0.78rem;
  }
  input,
  select {
    padding: 0.45rem;
    border: 1px solid #ccc;
    border-radius: 4px;
  }
  fieldset {
    border: 1px solid #ddd;
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  legend {
    font-size: 0.8rem;
    color: #666;
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
  .grants {
    background: #fff;
    padding: 1.25rem;
    border-radius: 6px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 0.75rem;
  }
  th,
  td {
    text-align: left;
    padding: 0.4rem 0.5rem;
    font-size: 0.85rem;
    border-bottom: 1px solid #eee;
  }
  .add-grant {
    display: flex;
    gap: 0.6rem;
    align-items: flex-start;
    flex-wrap: wrap;
    background: none;
    padding: 0;
  }
  .add-grant select {
    flex: 0 0 160px;
  }
  .cap-checks {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  button.link.danger {
    color: #c0392b;
    border: none;
    background: none;
    cursor: pointer;
  }
  .empty {
    text-align: center;
    color: #888;
  }
  .hint {
    font-size: 0.78rem;
    color: #777;
  }
  .error {
    color: #c0392b;
  }
</style>
