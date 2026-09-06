<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import RuleEditor from '../lib/components/RuleEditor.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import SeverityPatternEditor from '../lib/components/SeverityPatternEditor.svelte'
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
  let searchIndexingEnabled = false
  let username = ''
  let password = ''
  let privateKey = ''

  let agentToken = ''
  let generatingToken = false
  let tokenError = ''

  // Folders are purely organizational (name + optional parent — never a
  // host or protocol of their own); nesting is unlimited via
  // parent_folder_id. Both customers and folders can be created inline
  // here rather than forcing a detour through a separate admin page that
  // doesn't exist yet.
  const NEW_OPTION = '__new__'
  let showNewCustomerForm = false
  let newCustomerName = ''
  let creatingCustomer = false
  let customerCreateError = ''

  let showNewFolderForm = false
  let newFolderName = ''
  let newFolderParentId: number | null = null
  let creatingFolder = false
  let folderCreateError = ''

  // Native <select> elements track their own displayed value on user
  // interaction; a one-way `value={...}` prop only re-syncs it when the
  // bound expression actually changes, which it doesn't here when "+
  // Create new..." is picked (customerId/folderId are deliberately left
  // alone) — so the DOM is reset by hand instead, right after it happens.
  let customerSelectEl: HTMLSelectElement
  let folderSelectEl: HTMLSelectElement

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
  $: customerId, (showNewFolderForm = false)

  // Folders come back flat; nest them depth-first so the select can show
  // hierarchy (indentation) for arbitrarily deep trees.
  function folderOptions(all: Folder[]): { folder: Folder; depth: number }[] {
    const byParent = new Map<number | null, Folder[]>()
    for (const f of all) {
      const key = f.parent_folder_id
      if (!byParent.has(key)) byParent.set(key, [])
      byParent.get(key)!.push(f)
    }
    for (const list of byParent.values()) list.sort((a, b) => a.name.localeCompare(b.name))

    const result: { folder: Folder; depth: number }[] = []
    function walk(parentId: number | null, depth: number) {
      for (const f of byParent.get(parentId) ?? []) {
        result.push({ folder: f, depth })
        walk(f.id, depth + 1)
      }
    }
    walk(null, 0)
    return result
  }

  $: nestedFolders = folderOptions(folders)

  function handleCustomerSelectChange(event: Event) {
    const value = (event.target as HTMLSelectElement).value
    if (value === NEW_OPTION) {
      showNewCustomerForm = true
      if (customerSelectEl) customerSelectEl.value = customerId === null ? '' : String(customerId)
      return
    }
    showNewCustomerForm = false
    customerId = value === '' ? null : Number(value)
  }

  async function createCustomer() {
    const name = newCustomerName.trim()
    if (!name) return
    creatingCustomer = true
    customerCreateError = ''
    try {
      const created = await api.post<Customer>('/customers', { name })
      customers = [...customers, created].sort((a, b) => a.name.localeCompare(b.name))
      customerId = created.id
      newCustomerName = ''
      showNewCustomerForm = false
    } catch (err) {
      customerCreateError = err instanceof ApiError ? err.detail : 'Failed to create customer'
    } finally {
      creatingCustomer = false
    }
  }

  function handleFolderSelectChange(event: Event) {
    const value = (event.target as HTMLSelectElement).value
    if (value === NEW_OPTION) {
      newFolderParentId = folderId
      showNewFolderForm = true
      if (folderSelectEl) folderSelectEl.value = folderId === null ? '' : String(folderId)
      return
    }
    showNewFolderForm = false
    folderId = value === '' ? null : Number(value)
  }

  async function createFolder() {
    const name = newFolderName.trim()
    if (!name || customerId === null) return
    creatingFolder = true
    folderCreateError = ''
    try {
      const created = await api.post<Folder>('/folders', {
        name,
        customer_id: customerId,
        parent_folder_id: newFolderParentId,
      })
      folders = [...folders, created]
      folderId = created.id
      newFolderName = ''
      showNewFolderForm = false
    } catch (err) {
      folderCreateError = err instanceof ApiError ? err.detail : 'Failed to create folder'
    } finally {
      creatingFolder = false
    }
  }

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
          searchIndexingEnabled = source.search_indexing_enabled
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
          search_indexing_enabled: searchIndexingEnabled,
          ...(credential ? { credential } : {}),
        })
        push(`/settings/sources/${created.id}`)
      } else {
        await api.patch(`/sources/${sourceId}`, {
          name,
          customer_id: customerId,
          folder_id: folderId,
          host,
          port,
          base_path: basePath,
          enabled,
          search_indexing_enabled: searchIndexingEnabled,
          ...(credential ? { credential } : {}),
        })
        push('/settings/sources')
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

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>{isNew ? 'New source' : `Edit ${name}`}</h1>
    <button class="btn btn-ghost" on:click={() => push('/settings/sources')}
      >← back to sources</button
    >
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
          <select
            class="input"
            bind:this={customerSelectEl}
            value={customerId ?? ''}
            on:change={handleCustomerSelectChange}
          >
            <option value="">— none (top-level) —</option>
            {#each customers as customer (customer.id)}
              <option value={customer.id}>{customer.name}</option>
            {/each}
            <option value={NEW_OPTION}>+ Create new customer…</option>
          </select>
        </label>
        <label>
          Folder
          <select
            class="input"
            bind:this={folderSelectEl}
            value={folderId ?? ''}
            on:change={handleFolderSelectChange}
            disabled={customerId === null}
          >
            <option value="">— none —</option>
            {#each nestedFolders as { folder, depth } (folder.id)}
              <option value={folder.id}>{'—'.repeat(depth)}{depth > 0 ? ' ' : ''}{folder.name}</option>
            {/each}
            <option value={NEW_OPTION}>+ Create new folder…</option>
          </select>
        </label>
      </div>

      {#if showNewCustomerForm}
        <div class="inline-create">
          <input
            class="input"
            placeholder="New customer name"
            bind:value={newCustomerName}
            on:keydown={(e) => e.key === 'Enter' && (e.preventDefault(), createCustomer())}
          />
          <button
            type="button"
            class="btn btn-ghost"
            on:click={createCustomer}
            disabled={creatingCustomer || !newCustomerName.trim()}
          >
            {creatingCustomer ? 'Creating…' : 'Create'}
          </button>
          <button
            type="button"
            class="btn btn-ghost"
            on:click={() => {
              showNewCustomerForm = false
              newCustomerName = ''
              customerCreateError = ''
            }}
          >
            Cancel
          </button>
        </div>
        {#if customerCreateError}<p class="error indent">{customerCreateError}</p>{/if}
      {/if}

      {#if showNewFolderForm}
        <div class="inline-create">
          <input
            class="input"
            placeholder="New folder name"
            bind:value={newFolderName}
            on:keydown={(e) => e.key === 'Enter' && (e.preventDefault(), createFolder())}
          />
          <select class="input" bind:value={newFolderParentId}>
            <option value={null}>— top-level (no parent folder) —</option>
            {#each nestedFolders as { folder, depth } (folder.id)}
              <option value={folder.id}>{'—'.repeat(depth)}{depth > 0 ? ' ' : ''}{folder.name}</option>
            {/each}
          </select>
          <button
            type="button"
            class="btn btn-ghost"
            on:click={createFolder}
            disabled={creatingFolder || !newFolderName.trim()}
          >
            {creatingFolder ? 'Creating…' : 'Create'}
          </button>
          <button
            type="button"
            class="btn btn-ghost"
            on:click={() => {
              showNewFolderForm = false
              newFolderName = ''
              folderCreateError = ''
            }}
          >
            Cancel
          </button>
        </div>
        <p class="hint indent">
          Folders are purely organizational — a nested group of sources, not a host of their own.
          Nest as deep as you like.
        </p>
        {#if folderCreateError}<p class="error indent">{folderCreateError}</p>{/if}
      {/if}

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

      <label class="checkbox">
        <input type="checkbox" bind:checked={searchIndexingEnabled} />
        Include in full-text search
      </label>
      <p class="hint indent">
        Off by default — stores short per-line snippets of this source's rule-visible content in a
        local search index so it shows up under Search, refreshed periodically in the background.
      </p>

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
      <p class="hint override-hint">
        Adding a pattern below overrides the global severity indicators (Settings → Severity
        indicators) for this source specifically — its own set replaces the global one entirely
        rather than adding to it. Leave empty to keep using the global defaults.
      </p>
      <SeverityPatternEditor
        baseUrl={`/sources/${sourceId}/severity-patterns`}
        readOnly={source?.is_system ?? false}
      />
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
  .inline-create {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin-top: -0.4rem;
  }
  .inline-create .input {
    width: auto;
    flex: 1;
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
  .hint.indent,
  .error.indent {
    margin: -0.5rem 0 0;
    font-size: 0.78rem;
  }
  .override-hint {
    font-size: 0.78rem;
    margin: 0;
  }
</style>
