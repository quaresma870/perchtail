<script lang="ts">
  import { onMount, tick } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import FolderManagerRow from '../lib/components/FolderManagerRow.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { nestFoldersFlat, nestFoldersTree } from '../lib/folder-tree'
  import type { Customer, Folder } from '../lib/types'

  let customers: Customer[] = []
  let selectedCustomerId: number | null = null
  let folders: Folder[] = []
  let loading = true
  let error = ''

  let newFolderName = ''
  let newFolderParentId: number | null = null
  let newFolderInput: HTMLInputElement | null = null
  let creating = false
  let createError = ''

  let renamingId: number | null = null
  let renameValue = ''
  let errorsById: Record<number, string> = {}

  $: tree = nestFoldersTree(folders)
  $: flatOptions = nestFoldersFlat(folders)

  async function loadFolders() {
    if (selectedCustomerId === null) {
      folders = []
      return
    }
    try {
      folders = await api.get<Folder[]>(`/folders?customer_id=${selectedCustomerId}`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load folders'
    }
  }

  function selectCustomer(id: number) {
    selectedCustomerId = id
    renamingId = null
    errorsById = {}
    newFolderParentId = null
    loadFolders()
  }

  onMount(async () => {
    loading = true
    error = ''
    try {
      customers = await api.get<Customer[]>('/customers')
      if (customers.length > 0) {
        selectedCustomerId = customers[0].id
        await loadFolders()
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load customers'
    } finally {
      loading = false
    }
  })

  async function createFolder() {
    const name = newFolderName.trim()
    if (!name || selectedCustomerId === null) return
    creating = true
    createError = ''
    try {
      const created = await api.post<Folder>('/folders', {
        name,
        customer_id: selectedCustomerId,
        parent_folder_id: newFolderParentId,
      })
      folders = [...folders, created]
      newFolderName = ''
      newFolderParentId = null
    } catch (err) {
      createError = err instanceof ApiError ? err.detail : 'Failed to create folder'
    } finally {
      creating = false
    }
  }

  async function startAddChild(parentId: number) {
    newFolderParentId = parentId
    newFolderName = ''
    createError = ''
    await tick()
    newFolderInput?.focus()
    newFolderInput?.scrollIntoView({ block: 'nearest' })
  }

  function startRename(id: number) {
    const folder = folders.find((f) => f.id === id)
    if (!folder) return
    renamingId = id
    renameValue = folder.name
    errorsById = { ...errorsById, [id]: '' }
  }

  function cancelRename() {
    renamingId = null
  }

  async function submitRename(id: number) {
    const name = renameValue.trim()
    if (!name) return
    try {
      const updated = await api.patch<Folder>(`/folders/${id}`, { name })
      folders = folders.map((f) => (f.id === id ? updated : f))
      renamingId = null
    } catch (err) {
      errorsById = {
        ...errorsById,
        [id]: err instanceof ApiError ? err.detail : 'Failed to rename folder',
      }
    }
  }

  async function moveFolder(id: number, parentId: number | null) {
    errorsById = { ...errorsById, [id]: '' }
    try {
      const updated = await api.patch<Folder>(`/folders/${id}`, { parent_folder_id: parentId })
      folders = folders.map((f) => (f.id === id ? updated : f))
    } catch (err) {
      errorsById = {
        ...errorsById,
        [id]: err instanceof ApiError ? err.detail : 'Failed to move folder',
      }
    }
  }

  async function deleteFolder(id: number) {
    errorsById = { ...errorsById, [id]: '' }
    try {
      await api.delete(`/folders/${id}`)
      folders = folders.filter((f) => f.id !== id)
    } catch (err) {
      errorsById = {
        ...errorsById,
        [id]: err instanceof ApiError ? err.detail : 'Failed to delete folder',
      }
    }
  }
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Folders</h1>
  </div>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if customers.length === 0}
    <p class="hint">
      No customers yet — create one from a source's editor (Settings → Sources → + Add source)
      before organizing folders.
    </p>
  {:else}
    <div class="customer-picker">
      <label for="customer-select">Customer</label>
      <select
        id="customer-select"
        class="input"
        value={selectedCustomerId}
        on:change={(e) => selectCustomer(Number((e.target as HTMLSelectElement).value))}
      >
        {#each customers as customer (customer.id)}
          <option value={customer.id}>{customer.name}</option>
        {/each}
      </select>
    </div>

    <div class="card">
      {#if tree.length === 0}
        <p class="empty">No folders yet for this customer.</p>
      {:else}
        <div class="tree">
          {#each tree as node (node.folder.id)}
            <FolderManagerRow
              {node}
              {flatOptions}
              {renamingId}
              {renameValue}
              {errorsById}
              on:startRename={(e) => startRename(e.detail.id)}
              on:renameInput={(e) => (renameValue = e.detail.value)}
              on:submitRename={(e) => submitRename(e.detail.id)}
              on:cancelRename={cancelRename}
              on:move={(e) => moveFolder(e.detail.id, e.detail.parentId)}
              on:addChild={(e) => startAddChild(e.detail.parentId)}
              on:delete={(e) => deleteFolder(e.detail.id)}
            />
          {/each}
        </div>
      {/if}

      <form class="add-form" on:submit|preventDefault={createFolder}>
        <input
          class="input"
          type="text"
          placeholder="New folder name"
          bind:value={newFolderName}
          bind:this={newFolderInput}
        />
        <select class="input parent-select" bind:value={newFolderParentId}>
          <option value={null}>(top level)</option>
          {#each flatOptions as opt (opt.folder.id)}
            <option value={opt.folder.id}>{'—'.repeat(opt.depth)} {opt.folder.name}</option>
          {/each}
        </select>
        <button class="btn btn-primary" type="submit" disabled={creating || !newFolderName.trim()}>
          {creating ? 'Creating…' : '+ New folder'}
        </button>
      </form>
      {#if createError}
        <p class="error">{createError}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .customer-picker {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 1rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .customer-picker .input {
    width: auto;
    min-width: 220px;
  }
  .card {
    padding: 1rem 1.25rem;
  }
  .tree {
    margin-bottom: 1rem;
  }
  .add-form {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border-soft);
  }
  .add-form .input {
    width: auto;
    flex: 1;
    min-width: 180px;
  }
  .parent-select {
    max-width: 220px;
    font-size: 0.85rem;
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 1.5rem 0;
  }
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
