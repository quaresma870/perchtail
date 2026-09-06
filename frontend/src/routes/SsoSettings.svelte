<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type { ConnectionCheckResult, GroupRoleMapping, Role, SSOProvider } from '../lib/types'

  let provider: SSOProvider | null = null
  let loading = true
  let saving = false
  let testing = false
  let error = ''
  let testResult: ConnectionCheckResult | null = null

  let name = ''
  let issuer = ''
  let clientId = ''
  let clientSecret = ''
  let scopes = 'openid email profile'
  let groupClaim = ''
  let enabled = false

  let mappings: GroupRoleMapping[] = []
  let roles: Role[] = []
  let mappingsError = ''
  let newMappingGroup = ''
  let newMappingRoleId: number | '' = ''
  let creatingMapping = false

  function loadFormFrom(p: SSOProvider) {
    name = p.name
    issuer = p.issuer
    clientId = p.client_id
    clientSecret = ''
    scopes = p.scopes
    groupClaim = p.group_claim ?? ''
    enabled = p.enabled
  }

  async function load() {
    loading = true
    error = ''
    try {
      const providers = await api.get<SSOProvider[]>('/sso')
      provider = providers[0] ?? null
      if (provider) {
        loadFormFrom(provider)
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load SSO settings'
    } finally {
      loading = false
    }
  }

  async function loadMappings() {
    mappingsError = ''
    try {
      ;[mappings, roles] = await Promise.all([
        api.get<GroupRoleMapping[]>('/sso/group-mappings'),
        api.get<Role[]>('/roles'),
      ])
    } catch (err) {
      mappingsError = err instanceof ApiError ? err.detail : 'Failed to load group mappings'
    }
  }

  onMount(() => {
    load()
    loadMappings()
  })

  async function handleSubmit() {
    error = ''
    saving = true
    testResult = null
    try {
      if (provider) {
        const payload: Record<string, unknown> = {
          name,
          issuer,
          client_id: clientId,
          scopes,
          group_claim: groupClaim,
          enabled,
        }
        if (clientSecret) payload.client_secret = clientSecret
        provider = await api.patch<SSOProvider>(`/sso/${provider.id}`, payload)
      } else {
        provider = await api.post<SSOProvider>('/sso', {
          name,
          issuer,
          client_id: clientId,
          client_secret: clientSecret,
          scopes,
          group_claim: groupClaim || null,
          enabled,
        })
      }
      loadFormFrom(provider)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to save SSO settings'
    } finally {
      saving = false
    }
  }

  async function createMapping() {
    if (newMappingRoleId === '') return
    creatingMapping = true
    mappingsError = ''
    try {
      const order = mappings.length ? Math.max(...mappings.map((m) => m.order)) + 1 : 0
      await api.post('/sso/group-mappings', {
        order,
        group_name: newMappingGroup,
        role_id: newMappingRoleId,
      })
      newMappingGroup = ''
      newMappingRoleId = ''
      await loadMappings()
    } catch (err) {
      mappingsError = err instanceof ApiError ? err.detail : 'Failed to create mapping'
    } finally {
      creatingMapping = false
    }
  }

  async function deleteMapping(mapping: GroupRoleMapping) {
    if (!confirm(`Remove the mapping for group "${mapping.group_name}"?`)) return
    try {
      await api.delete(`/sso/group-mappings/${mapping.id}`)
      await loadMappings()
    } catch (err) {
      mappingsError = err instanceof ApiError ? err.detail : 'Failed to delete mapping'
    }
  }

  async function handleTest() {
    if (!provider) return
    testing = true
    testResult = null
    error = ''
    try {
      testResult = await api.post<ConnectionCheckResult>(`/sso/${provider.id}/test`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Test connection failed'
    } finally {
      testing = false
    }
  }

  async function handleDelete() {
    if (!provider) return
    if (!confirm(`Remove SSO provider "${provider.name}"? Existing SSO users keep their accounts.`)) {
      return
    }
    try {
      await api.delete(`/sso/${provider.id}`)
      provider = null
      name = ''
      issuer = ''
      clientId = ''
      clientSecret = ''
      scopes = 'openid email profile'
      groupClaim = ''
      enabled = false
      testResult = null
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to remove SSO provider'
    }
  }
</script>

<SettingsNav />

<div class="page">
  <h1>SSO settings</h1>
  <p class="hint">
    One OIDC provider can be configured at a time (Azure AD/Entra ID, Okta, Google Workspace,
    Keycloak/Authentik — anything speaking standard OIDC). Local accounts keep working
    alongside it. A first-time SSO sign-in auto-provisions a no-access account — assign it a
    real role from the <a href="#/settings/users">Users</a> page afterward.
  </p>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <form class="card" on:submit|preventDefault={handleSubmit}>
      <label>
        Display name
        <input class="input" bind:value={name} placeholder="Corporate SSO" required />
      </label>
      <label>
        Issuer URL
        <input
          class="input mono"
          bind:value={issuer}
          placeholder="https://login.example.com/tenant-id"
          required
        />
      </label>
      <label>
        Client ID
        <input class="input mono" bind:value={clientId} required />
      </label>
      <label>
        Client secret
        <input
          class="input mono"
          type="password"
          bind:value={clientSecret}
          placeholder={provider ? 'Leave blank to keep the current secret' : ''}
          required={!provider}
        />
      </label>
      <label>
        Scopes
        <input class="input mono" bind:value={scopes} />
      </label>
      <label>
        Group claim <span class="optional">(optional)</span>
        <input
          class="input mono"
          bind:value={groupClaim}
          placeholder="groups"
        />
        <span class="field-hint">
          Name of the ID token claim carrying the user's IdP group memberships. Set this to
          auto-map groups to roles below — leave blank to disable auto-mapping.
        </span>
      </label>

      <div class="switch-row">
        <label class="switch">
          <input type="checkbox" bind:checked={enabled} />
          <span class="switch-track"></span>
        </label>
        <span>Enabled</span>
      </div>

      <div class="actions">
        <button class="btn btn-primary" type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {#if provider}
          <button class="btn btn-ghost" type="button" disabled={testing} on:click={handleTest}>
            {testing ? 'Testing…' : 'Test connection'}
          </button>
          <button class="btn btn-ghost danger" type="button" on:click={handleDelete}>
            Remove
          </button>
        {/if}
      </div>

      {#if testResult}
        <p class="test-result" class:ok={testResult.ok} class:fail={!testResult.ok}>
          {testResult.ok ? '✓' : '✕'}
          {testResult.detail}
        </p>
      {/if}
    </form>

    <div class="card mappings-card">
      <div class="mappings-header">
        <h2>Group → role mapping</h2>
        <p class="hint">
          Auto-assigns a role on every SSO login based on the user's IdP groups (needs "Group
          claim" set above). Evaluated in order, last match wins — same rule as source rules.
          Overrides any role assigned directly in PerchTail as long as the user's groups still
          match, so remove a mapping (or the user from that group) to stop the sync.
        </p>
      </div>

      {#if mappingsError}
        <p class="error">{mappingsError}</p>
      {/if}

      {#if mappings.length > 0}
        <ul class="mapping-list">
          {#each mappings as mapping (mapping.id)}
            <li class="mapping-row">
              <span class="order">{mapping.order}</span>
              <code class="group-name">{mapping.group_name}</code>
              <span class="arrow">→</span>
              <span class="badge badge-accent">{mapping.role_name}</span>
              <button
                class="btn btn-ghost danger"
                type="button"
                on:click={() => deleteMapping(mapping)}
              >
                Remove
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <form class="mapping-create" on:submit|preventDefault={createMapping}>
        <input
          class="input mono"
          bind:value={newMappingGroup}
          placeholder="IdP group name"
          required
        />
        <select class="input" bind:value={newMappingRoleId} required>
          <option value="" disabled>Role…</option>
          {#each roles as role (role.id)}
            <option value={role.id}>{role.name}</option>
          {/each}
        </select>
        <button class="btn btn-ghost" type="submit" disabled={creatingMapping}>
          {creatingMapping ? 'Adding…' : '+ Add mapping'}
        </button>
      </form>
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 640px;
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .hint a {
    color: var(--accent-hover);
  }
  form {
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
  .mono {
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }
  .switch-row {
    flex-direction: row;
    align-items: center;
    gap: 0.7rem;
    color: var(--text);
    font-size: 0.88rem;
  }
  .actions {
    display: flex;
    gap: 0.6rem;
    align-items: center;
  }
  button.danger {
    color: var(--danger);
  }
  .test-result {
    margin: 0;
    font-size: 0.85rem;
    padding: 0.5rem 0.7rem;
    border-radius: var(--radius-sm);
  }
  .test-result.ok {
    background: var(--success-soft);
    color: var(--success);
  }
  .test-result.fail {
    background: var(--danger-soft);
    color: var(--danger);
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
  .optional {
    font-weight: 400;
    color: var(--text-faint);
  }
  .field-hint {
    font-size: 0.78rem;
    color: var(--text-faint);
    line-height: 1.4;
  }
  .mappings-card {
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .mappings-header {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .mappings-header h2 {
    font-size: 1rem;
    margin: 0;
    color: var(--text);
  }
  .mapping-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .mapping-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.7rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
  .mapping-row .order {
    font-size: 0.75rem;
    color: var(--text-faint);
    font-family: var(--font-mono);
    min-width: 1.2rem;
  }
  .mapping-row .group-name {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    color: var(--text);
  }
  .mapping-row .arrow {
    color: var(--text-faint);
  }
  .mapping-row .badge {
    margin-right: auto;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .mapping-create {
    display: flex;
    flex-direction: row;
    gap: 0.6rem;
    padding: 0;
  }
  .mapping-create .input {
    flex: 1;
  }
</style>
