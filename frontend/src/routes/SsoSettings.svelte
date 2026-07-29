<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import type { ConnectionCheckResult, SSOProvider } from '../lib/types'

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
  let enabled = false

  function loadFormFrom(p: SSOProvider) {
    name = p.name
    issuer = p.issuer
    clientId = p.client_id
    clientSecret = ''
    scopes = p.scopes
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

  onMount(load)

  async function handleSubmit() {
    error = ''
    saving = true
    testResult = null
    try {
      if (provider) {
        const payload: Record<string, unknown> = { name, issuer, client_id: clientId, scopes, enabled }
        if (clientSecret) payload.client_secret = clientSecret
        provider = await api.patch<SSOProvider>(`/sso/${provider.id}`, payload)
      } else {
        provider = await api.post<SSOProvider>('/sso', {
          name,
          issuer,
          client_id: clientId,
          client_secret: clientSecret,
          scopes,
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
      enabled = false
      testResult = null
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to remove SSO provider'
    }
  }
</script>

<div class="page">
  <h1>SSO settings</h1>
  <p class="hint">
    One OIDC provider can be configured at a time (Azure AD/Entra ID, Okta, Google Workspace,
    Keycloak/Authentik — anything speaking standard OIDC). Local accounts keep working
    alongside it. A first-time SSO sign-in auto-provisions a no-access account — assign it a
    real role from the <a href="#/users">Users</a> page afterward.
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
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 560px;
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
</style>
