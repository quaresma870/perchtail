<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import RuleEditor from '../lib/components/RuleEditor.svelte'
  import type { AgentTokenResult, Customer, Folder, Protocol, Source } from '../lib/types'

  export let params: { id?: string } = {}

  const isNew = !params.id
  const sourceId = params.id ? Number(params.id) : null

  let source: Source | null = null
  let customers: Customer[] = []
  let folders: Folder[] = []
  let loading = !isNew
  let saving = false
  let error = ''

  let name = ''
  let customerId: number | null = null
  let folderId: number | null = null
  let protocol: Protocol = 'ssh'
  let host = ''
  let port: number | null = null
  let basePath = ''
  let enabled = true
  let username = ''
  let password = ''
  let privateKey = ''

  let agentToken = ''
  let generatingToken = false
  let tokenError = ''

  async function loadCustomers() {
    customers = await api.get<Customer[]>('/customers')
  }

  async function loadFolders() {
    if (customerId === null) {
      folders = []
      return
    }
    folders = await api.get<Folder[]>(`/folders?customer_id=${customerId}`)
  }

  $: customerId, loadFolders()

  onMount(async () => {
    try {
      await loadCustomers()
      if (sourceId !== null) {
        source = await api.get<Source>(`/sources/${sourceId}`)
        if (source) {
          name = source.name
          customerId = source.customer_id
          folderId = source.folder_id
          protocol = source.protocol
          host = source.host
          port = source.port
          basePath = source.base_path
          enabled = source.enabled
        }
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load source'
    } finally {
      loading = false
    }
  })

  function buildCredential(): Record<string, string> | null {
    if (protocol === 'local' || protocol === 'agent') return null
    const cred: Record<string, string> = {}
    if (username) cred.username = username
    if (protocol === 'ssh' && privateKey) cred.private_key = privateKey
    else if (password) cred.password = password
    return Object.keys(cred).length > 0 ? cred : null
  }

  async function handleSubmit() {
    error = ''
    saving = true
    try {
      const credential = buildCredential()
      if (isNew) {
        const created = await api.post<Source>('/sources', {
          name,
          customer_id: customerId,
          folder_id: folderId,
          protocol,
          host,
          port,
          base_path: basePath,
          enabled,
          ...(credential ? { credential } : {}),
        })
        push(`/sources/${created.id}`)
      } else {
        await api.patch(`/sources/${sourceId}`, {
          name,
          customer_id: customerId,
          folder_id: folderId,
          host,
          port,
          base_path: basePath,
          enabled,
          ...(credential ? { credential } : {}),
        })
        push('/sources')
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to save source'
    } finally {
      saving = false
    }
  }

  async function generateAgentToken() {
    if (sourceId === null) return
    tokenError = ''
    generatingToken = true
    try {
      const result = await api.post<AgentTokenResult>(`/sources/${sourceId}/agent-token`)
      agentToken = result.token
      if (source) source = { ...source, has_agent_token: true }
    } catch (err) {
      tokenError = err instanceof ApiError ? err.detail : 'Failed to generate token'
    } finally {
      generatingToken = false
    }
  }
</script>

<div class="page">
  <div class="header">
    <h1>{isNew ? 'New source' : `Edit ${name}`}</h1>
    <button class="btn btn-ghost" on:click={() => push('/sources')}>← back to sources</button>
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

      <div class="row">
        <label>
          Customer
          <select class="input" bind:value={customerId}>
            <option value={null}>— none (top-level) —</option>
            {#each customers as customer (customer.id)}
              <option value={customer.id}>{customer.name}</option>
            {/each}
          </select>
        </label>
        <label>
          Folder
          <select class="input" bind:value={folderId} disabled={customerId === null}>
            <option value={null}>— none —</option>
            {#each folders as folder (folder.id)}
              <option value={folder.id}>{folder.name}</option>
            {/each}
          </select>
        </label>
      </div>

      <label>
        Protocol
        <select class="input" bind:value={protocol} disabled={!isNew}>
          <option value="ssh">SSH / SFTP</option>
          <option value="smb">SMB</option>
          <option value="winrm">WinRM</option>
          <option value="local">Local disk</option>
          <option value="agent">Agent (push)</option>
        </select>
      </label>

      <div class="row">
        <label class="grow">
          Host
          <input
            class="input"
            bind:value={host}
            required
            placeholder={protocol === 'agent' ? 'friendly name for the agent host' : undefined}
          />
        </label>
        {#if protocol !== 'agent'}
          <label class="narrow">
            Port
            <input class="input" type="number" bind:value={port} placeholder="default" />
          </label>
        {/if}
      </div>

      <label>
        Base path
        <input
          class="input"
          bind:value={basePath}
          required
          placeholder={protocol === 'agent'
            ? 'documented for reference — the agent enforces its own root'
            : '/var/log/appname'}
        />
      </label>

      <label class="checkbox">
        <input type="checkbox" bind:checked={enabled} />
        Enabled
      </label>

      {#if protocol === 'agent'}
        <fieldset>
          <legend>Agent enrollment</legend>
          {#if isNew}
            <p class="hint">Save the source first, then generate an enrollment token for the agent.</p>
          {:else}
            <p class="hint">
              Set <code>PERCHTAIL_AGENT_TOKEN</code> on the agent to the token below — it's shown
              only once. Generating a new token invalidates the previous one.
            </p>
            {#if tokenError}
              <p class="error">{tokenError}</p>
            {/if}
            <button
              type="button"
              class="btn btn-ghost"
              on:click={generateAgentToken}
              disabled={generatingToken}
            >
              {generatingToken
                ? 'Generating…'
                : source?.has_agent_token
                  ? 'Regenerate token'
                  : 'Generate token'}
            </button>
            {#if agentToken}
              <code class="token-box">{agentToken}</code>
            {/if}
          {/if}
        </fieldset>
      {:else if protocol !== 'local'}
        <fieldset>
          <legend>Credential {isNew ? '' : '(leave blank to keep current)'}</legend>
          <label>
            Username
            <input class="input" bind:value={username} autocomplete="off" />
          </label>
          {#if protocol === 'ssh'}
            <label>
              Private key (leave blank to use password instead)
              <textarea class="input" rows="4" bind:value={privateKey}></textarea>
            </label>
          {/if}
          <label>
            Password
            <input class="input" type="password" bind:value={password} autocomplete="new-password" />
          </label>
        </fieldset>
      {/if}

      <button class="btn btn-primary" type="submit" disabled={saving}>
        {saving ? 'Saving…' : 'Save'}
      </button>
    </form>

    {#if !isNew && sourceId !== null}
      <RuleEditor {sourceId} readOnly={source?.is_system ?? false} />
    {/if}
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 640px;
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
  label.checkbox {
    flex-direction: row;
    align-items: center;
    gap: 0.5rem;
    color: var(--text);
  }
  input,
  select,
  textarea {
    width: 100%;
  }
  .row {
    display: flex;
    gap: 0.75rem;
  }
  .row .grow {
    flex: 1;
  }
  .row .narrow {
    flex: 0 0 110px;
  }
  fieldset {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    padding: 0.9rem;
  }
  legend {
    font-size: 0.8rem;
    color: var(--text-faint);
    padding: 0 0.3rem;
  }
  .token-box {
    display: block;
    padding: 0.6rem 0.75rem;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    word-break: break-all;
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
