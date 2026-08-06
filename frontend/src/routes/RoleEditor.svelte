<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type {
    Customer,
    Folder,
    GlobalCapability,
    Role,
    RoleGrant,
    Source,
    Capability,
    ScopeType,
  } from '../lib/types'

  export let params: { id?: string } = {}

  const isNew = !params.id
  const roleId = params.id ? Number(params.id) : null

  const ALL_GLOBAL_CAPS: { key: GlobalCapability; label: string }[] = [
    { key: 'create_source', label: 'Create new sources' },
    { key: 'manage_users', label: 'Manage users' },
    { key: 'manage_roles', label: 'Manage roles' },
    { key: 'manage_sso', label: 'Manage SSO settings' },
    { key: 'manage_system_settings', label: 'Manage system settings (feature toggles)' },
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
        push(`/settings/roles/${created.id}`)
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

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>{isNew ? 'New role' : `Role: ${name}`}</h1>
    <button class="btn btn-ghost" on:click={() => push('/settings/roles')}>← back to roles</button>
  </div>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <form class="card" on:submit|preventDefault={handleSubmit}>
      <label>
        Name
        <input class="input" bind:value={name} required />
      </label>

      <div class="switch-row">
        <label class="switch">
          <input type="checkbox" bind:checked={isSuperAdmin} disabled={!canEditSuperAdmin} />
          <span class="switch-track"></span>
        </label>
        <span>Super-admin (bypasses all grants, reaches system sources)</span>
      </div>
      {#if !canEditSuperAdmin}
        <p class="hint indent">Only a super-admin can grant or revoke this flag.</p>
      {/if}

      <fieldset>
        <legend>Global capabilities</legend>
        {#each ALL_GLOBAL_CAPS as cap}
          <div class="switch-row">
            <label class="switch">
              <input
                type="checkbox"
                checked={globalCaps.has(cap.key)}
                on:change={() => (globalCaps = toggle(globalCaps, cap.key))}
              />
              <span class="switch-track"></span>
            </label>
            <span>{cap.label}</span>
          </div>
        {/each}
      </fieldset>

      <button class="btn btn-primary" type="submit" disabled={saving}>
        {saving ? 'Saving…' : 'Save'}
      </button>
    </form>

    {#if !isNew}
      <div class="grants card">
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
                <td><span class="badge badge-muted scope-badge">{grant.scope_type}</span></td>
                <td class="target">{scopeLabel(grant)}</td>
                <td class="cap-list">
                  {#each grant.capabilities as cap}
                    <span class="badge badge-accent">{cap}</span>
                  {/each}
                  {#if grant.capabilities.length === 0}—{/if}
                </td>
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
          <select class="input" bind:value={newScopeType} on:change={() => (newScopeId = null)}>
            <option value="customer">customer</option>
            <option value="folder">folder</option>
            <option value="source">source</option>
          </select>
          <select class="input" bind:value={newScopeId}>
            <option value={null}>— pick —</option>
            {#each scopeOptions() as opt (opt.id)}
              <option value={opt.id}>{opt.label}</option>
            {/each}
          </select>
          <div class="cap-checks">
            {#each ALL_CAPS as cap}
              <label class="chip" class:chip-active={newCaps.has(cap)}>
                <input
                  type="checkbox"
                  checked={newCaps.has(cap)}
                  on:change={() => (newCaps = toggle(newCaps, cap))}
                />
                {cap}
              </label>
            {/each}
          </div>
          <button class="btn btn-primary" type="submit">Add grant</button>
        </form>
      </div>
    {/if}
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 760px;
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
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  h2 {
    font-size: 1rem;
    margin: 0 0 0.4rem;
    color: var(--text);
  }
  form.card {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    padding: 1.5rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .switch-row {
    flex-direction: row;
    align-items: center;
    gap: 0.7rem;
    color: var(--text);
    font-size: 0.88rem;
  }
  fieldset {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
    padding: 0.9rem;
  }
  legend {
    font-size: 0.8rem;
    color: var(--text-faint);
    padding: 0 0.3rem;
  }
  button[type='submit'] {
    align-self: flex-start;
    padding: 0.6rem 1.2rem;
  }
  .grants {
    padding: 1.5rem;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.75rem 0;
  }
  th,
  td {
    text-align: left;
    padding: 0.5rem 0.6rem;
    font-size: 0.85rem;
    border-bottom: 1px solid var(--border-soft);
  }
  th {
    color: var(--text-faint);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
  }
  .target {
    color: var(--text);
    font-weight: 500;
  }
  .scope-badge {
    text-transform: capitalize;
  }
  .cap-list {
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .badge-muted {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .add-grant {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    flex-wrap: wrap;
  }
  .add-grant select {
    flex: 0 0 160px;
  }
  .cap-checks {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.25rem 0.65rem;
    font-size: 0.75rem;
    color: var(--text-muted);
    cursor: pointer;
  }
  .chip input {
    accent-color: var(--accent);
  }
  .chip-active {
    border-color: var(--accent-border);
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  button.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0;
  }
  button.link.danger {
    color: var(--danger);
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 1.5rem 0;
  }
  .hint {
    font-size: 0.78rem;
    color: var(--text-faint);
    margin: 0;
  }
  .hint.indent {
    margin-left: 2.7rem;
  }
  .error {
    color: var(--danger);
  }
</style>
