<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import FolderPicker from '../lib/components/FolderPicker.svelte'
  import SeverityPatternEditor from '../lib/components/SeverityPatternEditor.svelte'
  import {
    basePathHint,
    basePathPlaceholder,
    canPickFolder,
    suggestRootLabel,
  } from '../lib/base-path'
  import { nestFoldersFlat } from '../lib/folder-tree'
  import { systemSettings } from '../lib/settings'
  import { justCreatedSourceId } from '../lib/source-created'
  import type {
    AgentTokenResult,
    Customer,
    Folder,
    Protocol,
    Source,
    SourceRoot,
  } from '../lib/types'

  export let params: { id?: string } = {}

  const isNew = !params.id
  const sourceId = params.id ? Number(params.id) : null

  let source: Source | null = null
  let customers: Customer[] = []
  let folders: Folder[] = []
  let loading = !isNew
  let saving = false
  let error = ''
  // Shown next to the Save button, where the user is looking (#206).
  let saveError = ''

  let name = ''
  let customerId: number | null = null
  let folderId: number | null = null
  let protocol: Protocol = 'ssh'
  let host = ''
  let port: number | null = null
  // The folder(s) this source reads. One entry is an ordinary source
  // (its path is the Base path); two or more each get a label and show up
  // as top-level folders in the viewer (#161).
  let roots: SourceRoot[] = [{ label: '', path: '' }]
  let hadRoots = false
  let originalPaths = ''
  let originalHostPort = ''
  // Index of the root whose Browse… picker is open.
  let pickerFor: number | null = null
  // Settings -> System -> Folders per source; a source that already has
  // more than the limit keeps them but can't add more.
  let hadRootsCount = 0
  $: rootsLimit = $systemSettings.source_max_roots
  let winrmScheme: 'https' | 'http' = 'https'
  let tlsCaPem = ''
  // JEA endpoint (#169): the PowerShell session configuration name; empty =
  // the WinRS shell.
  let winrmConfiguration = ''
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

  $: nestedFolders = nestFoldersFlat(folders)

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
          roots =
            source.roots.length > 0
              ? source.roots.map((r) => ({ ...r }))
              : [{ label: '', path: source.base_path }]
          hadRoots = source.roots.length > 0
          hadRootsCount = source.roots.length
          winrmScheme = source.winrm_effective_scheme ?? 'https'
          tlsCaPem = source.tls_ca_pem ?? ''
          winrmConfiguration = source.winrm_configuration ?? ''
          originalPaths = JSON.stringify(roots.map((r) => r.path))
          originalHostPort = `${source.host}:${source.port ?? ''}`
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

  $: multiRoot = roots.length > 1
  // A saved credential is only sent to the host and port it was saved for,
  // so pointing the source elsewhere needs it typed again.
  $: hostPortChanged = !isNew && originalHostPort !== '' && `${host}:${port ?? ''}` !== originalHostPort
  $: needsCredentialAgain =
    hostPortChanged && !!source?.has_credential && protocol !== 'agent' && protocol !== 'local'
  $: pathsChanged = !isNew && JSON.stringify(roots.map((r) => r.path)) !== originalPaths

  function addRoot() {
    if (roots.length === 1 && !roots[0].label) {
      roots[0].label = suggestRootLabel(roots[0].path, [])
    }
    roots = [...roots, { label: '', path: '' }]
  }

  function removeRoot(index: number) {
    roots = roots.filter((_, i) => i !== index)
    if (pickerFor !== null) pickerFor = null
  }

  function pickedFolder(index: number, path: string) {
    roots[index].path = path
    if (roots.length > 1 && !roots[index].label) {
      roots[index].label = suggestRootLabel(
        path,
        roots.filter((_, i) => i !== index).map((r) => r.label),
      )
    }
    pickerFor = null
  }

  function winrmPayload(): Record<string, string> {
    if (protocol !== 'winrm') return {}
    return {
      winrm_scheme: winrmScheme,
      tls_ca_pem: winrmScheme === 'https' ? tlsCaPem : '',
      winrm_configuration: winrmConfiguration.trim(),
    }
  }

  function locationPayload() {
    const cleaned = roots.map((r) => ({ label: r.label.trim(), path: r.path.trim() }))
    if (cleaned.length > 1) return { base_path: cleaned[0].path, roots: cleaned }
    // Sending a single root turns a multi-root source back into a plain one.
    return { base_path: cleaned[0].path, ...(hadRoots ? { roots: cleaned } : {}) }
  }

  async function handleSubmit() {
    error = ''
    saveError = ''
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
          ...locationPayload(),
          ...winrmPayload(),
          enabled,
          search_indexing_enabled: searchIndexingEnabled,
          ...(credential ? { credential } : {}),
        })
        // Land on the read-only overview, not back on this form (#157).
        justCreatedSourceId.set(created.id)
        push(`/settings/sources/${created.id}`)
      } else {
        await api.patch(`/sources/${sourceId}`, {
          name,
          customer_id: customerId,
          folder_id: folderId,
          host,
          port,
          ...locationPayload(),
          ...winrmPayload(),
          enabled,
          search_indexing_enabled: searchIndexingEnabled,
          ...(credential ? { credential } : {}),
        })
        push(`/settings/sources/${sourceId}`)
      }
    } catch (err) {
      saveError = err instanceof ApiError ? err.detail : 'Failed to save source'
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
    <button
      class="btn btn-ghost"
      on:click={() => push(isNew ? '/settings/sources' : `/settings/sources/${sourceId}`)}
      >{isNew ? '← back to sources' : '← back to source'}</button
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
        {#if protocol === 'winrm'}
          <label class="narrow">
            Scheme
            <select class="input" bind:value={winrmScheme}>
              <option value="https">HTTPS</option>
              <option value="http">HTTP</option>
            </select>
          </label>
        {/if}
        {#if protocol !== 'agent'}
          <label class="narrow">
            Port
            <input
              class="input"
              type="number"
              bind:value={port}
              placeholder={protocol === 'winrm' ? (winrmScheme === 'https' ? '5986' : '5985') : 'default'}
            />
          </label>
        {/if}
      </div>
      {#if protocol === 'winrm' && winrmScheme === 'https'}
        <label>
          CA certificate (PEM, optional)
          <textarea
            class="input mono"
            rows="3"
            wrap="off"
            bind:value={tlsCaPem}
            placeholder="-----BEGIN CERTIFICATE-----  for an internal CA or a self-signed listener"
            spellcheck="false"
            autocomplete="off"
            data-gramm="false"
          ></textarea>
        </label>
      {:else if protocol === 'winrm'}
        <p class="warning small">
          Plain HTTP: NTLM still encrypts the WinRM messages, but the server isn't authenticated.
          Prefer HTTPS.
        </p>
      {/if}
      {#if protocol === 'winrm'}
        <label>
          JEA endpoint (optional)
          <input
            class="input"
            bind:value={winrmConfiguration}
            placeholder="PerchTail"
            autocomplete="off"
            spellcheck="false"
          />
          <span class="hint small">
            The PowerShell endpoint registered with PerchTail's read-only JEA role
            (docs/examples/jea): the account then needs no administrator rights or remote
            shell, and can only list and read under the role's folders. Empty: a WinRS shell,
            which needs an account with remote-shell rights.
          </span>
        </label>
      {/if}

      <div class="roots">
        {#each roots as root, index (index)}
          <div class="root-row">
            {#if multiRoot}
              <label class="root-label">
                Label
                <input
                  class="input"
                  bind:value={root.label}
                  required
                  maxlength="64"
                  placeholder="e.g. IIS"
                />
              </label>
            {/if}
            <label class="grow">
              {index === 0 ? 'Base path' : 'Path'}
              <input
                class="input mono"
                bind:value={root.path}
                required={protocol !== 'agent'}
                placeholder={basePathPlaceholder(protocol)}
                spellcheck="false"
                autocomplete="off"
                data-gramm="false"
              />
            </label>
            {#if canPickFolder(protocol)}
              <button
                type="button"
                class="btn btn-ghost root-btn"
                disabled={!host.trim()}
                title={host.trim() ? 'Browse the host for a folder' : 'Enter the host first'}
                on:click={() => (pickerFor = pickerFor === index ? null : index)}>Browse…</button
              >
            {/if}
            {#if multiRoot}
              <button
                type="button"
                class="btn btn-ghost root-btn"
                aria-label={`Remove folder ${root.label || index + 1}`}
                on:click={() => removeRoot(index)}>✕</button
              >
            {/if}
          </div>
          {#if pickerFor === index}
            <FolderPicker
              {protocol}
              {host}
              {port}
              credential={buildCredential()}
              sourceId={isNew ? null : sourceId}
              winrm={protocol === 'winrm' ? winrmPayload() : null}
              startPath={root.path}
              on:pick={(e) => pickedFolder(index, e.detail)}
              on:close={() => (pickerFor = null)}
            />
          {/if}
        {/each}
        {#if basePathHint(protocol)}
          <p class="hint small">{basePathHint(protocol)}</p>
        {/if}
        {#if protocol !== 'agent' && roots.length < Math.max(rootsLimit, hadRootsCount)}
          <button type="button" class="btn btn-ghost add-root" on:click={addRoot}>
            + Add another folder
          </button>
          {#if multiRoot}
            <p class="hint small">
              Each folder appears in the viewer as a top-level folder named by its label. Rules
              see those names too: <code>**/*.log</code> covers every folder,
              <code>IIS/**</code> only one.
            </p>
          {/if}
        {/if}
        {#if pathsChanged}
          <p class="warning small">
            Rules are relative to the base path, so after saving they apply to what's at the new
            location — check they still match. The source's search index is cleared and rebuilt.
          </p>
        {/if}
      </div>

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
          <legend>Credential {isNew || needsCredentialAgain ? '' : '(leave blank to keep current)'}</legend>
          {#if needsCredentialAgain}
            <p class="warning small">
              The host or port changed: enter the credential again. The saved one is only sent to the
              host and port it was saved for.
            </p>
          {/if}
          <label>
            Username
            <input class="input" bind:value={username} autocomplete="off" />
          </label>
          {#if protocol === 'ssh'}
            <label>
              Private key: ed25519, ECDSA or RSA, without a passphrase (leave blank to
              use password instead)
              <textarea
                class="input mono"
                rows="4"
                wrap="off"
                bind:value={privateKey}
                spellcheck="false"
                autocomplete="off"
                autocapitalize="off"
                data-gramm="false"
                data-gramm_editor="false"
                data-enable-grammarly="false"
              ></textarea>
            </label>
          {/if}
          <label>
            Password
            <input
              class="input"
              type="password"
              bind:value={password}
              autocomplete="new-password"
              spellcheck="false"
              data-gramm="false"
              data-enable-grammarly="false"
            />
          </label>
        </fieldset>
      {/if}

      <div class="save-row">
        <button class="btn btn-primary" type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {#if saveError}
          <p class="error save-error" role="alert">{saveError}</p>
        {/if}
      </div>
    </form>

    {#if !isNew && sourceId !== null}
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
  /* Full width for text fields only -- checkboxes and radios keep their
     natural size, so they sit right next to their label (#158). */
  input:not([type='checkbox']):not([type='radio']),
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
  .save-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }
  .save-error {
    margin: 0;
    flex: 1;
    min-width: 14rem;
  }
  .error.indent {
    margin: -0.5rem 0 0;
    font-size: 0.78rem;
  }
  .roots {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .root-row {
    display: flex;
    gap: 0.5rem;
    align-items: flex-end;
  }
  .root-row .grow {
    flex: 1;
  }
  .root-label {
    flex: 0 0 140px;
  }
  .root-btn {
    flex: 0 0 auto;
  }
  .add-root {
    align-self: flex-start;
    font-size: 0.8rem;
    padding: 0.3rem 0.6rem;
  }
  .mono {
    font-family: var(--font-mono);
  }
  .small {
    margin: 0;
    font-size: 0.78rem;
  }
  .warning {
    color: var(--warning, #b58105);
  }
  .override-hint {
    font-size: 0.78rem;
    margin: 0;
  }
</style>
