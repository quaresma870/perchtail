<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import RuleEditor from '../lib/components/RuleEditor.svelte'
  import type { Customer, Folder, Protocol, Source } from '../lib/types'

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
    if (protocol === 'local') return null
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
</script>

<div class="page">
  <div class="header">
    <h1>{isNew ? 'New source' : `Edit ${name}`}</h1>
    <button class="link" on:click={() => push('/sources')}>← back to sources</button>
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

      <div class="row">
        <label>
          Customer
          <select bind:value={customerId}>
            <option value={null}>— none (top-level) —</option>
            {#each customers as customer (customer.id)}
              <option value={customer.id}>{customer.name}</option>
            {/each}
          </select>
        </label>
        <label>
          Folder
          <select bind:value={folderId} disabled={customerId === null}>
            <option value={null}>— none —</option>
            {#each folders as folder (folder.id)}
              <option value={folder.id}>{folder.name}</option>
            {/each}
          </select>
        </label>
      </div>

      <label>
        Protocol
        <select bind:value={protocol} disabled={!isNew}>
          <option value="ssh">SSH / SFTP</option>
          <option value="smb">SMB</option>
          <option value="winrm">WinRM</option>
          <option value="local">Local disk</option>
        </select>
      </label>

      <div class="row">
        <label class="grow">
          Host
          <input bind:value={host} required />
        </label>
        <label class="narrow">
          Port
          <input type="number" bind:value={port} placeholder="default" />
        </label>
      </div>

      <label>
        Base path
        <input bind:value={basePath} required placeholder="/var/log/appname" />
      </label>

      <label class="checkbox">
        <input type="checkbox" bind:checked={enabled} />
        Enabled
      </label>

      {#if protocol !== 'local'}
        <fieldset>
          <legend>Credential {isNew ? '' : '(leave blank to keep current)'}</legend>
          <label>
            Username
            <input bind:value={username} autocomplete="off" />
          </label>
          {#if protocol === 'ssh'}
            <label>
              Private key (leave blank to use password instead)
              <textarea rows="4" bind:value={privateKey}></textarea>
            </label>
          {/if}
          <label>
            Password
            <input type="password" bind:value={password} autocomplete="new-password" />
          </label>
        </fieldset>
      {/if}

      <button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
    </form>

    {#if !isNew && sourceId !== null}
      <RuleEditor {sourceId} readOnly={source?.is_system ?? false} />
    {/if}
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
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
    font-size: 1.3rem;
    margin: 0;
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
    gap: 0.85rem;
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
  input,
  select,
  textarea {
    padding: 0.45rem;
    border: 1px solid #ccc;
    border-radius: 4px;
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
    border: 1px solid #ddd;
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  legend {
    font-size: 0.8rem;
    color: #666;
    padding: 0 0.3rem;
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
  .error {
    color: #c0392b;
  }
</style>
