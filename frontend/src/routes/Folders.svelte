<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import FolderManagerRow from '../lib/components/FolderManagerRow.svelte'
  import Modal from '../lib/components/Modal.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { confirmAction } from '../lib/confirm'
  import { nestFoldersFlat, nestFoldersTree } from '../lib/folder-tree'
  import type { Customer, Folder } from '../lib/types'

  let customers: Customer[] = []
  let selectedCustomerId: number | null = null
  let folders: Folder[] = []
  let loading = true
  let error = ''
  let errorsById: Record<number, string> = {}

  // New folder, rename and move open in a popup (#301).
  type Editing =
    | { kind: 'create'; name: string; parentId: number | null }
    | { kind: 'rename'; id: number; name: string }
    | { kind: 'move'; id: number; name: string; parentId: number | null }
  let editing: Editing | null = null
  let saving = false
  let editError = ''

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
    errorsById = {}
    editing = null
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

  const folderById = (id: number) => folders.find((f) => f.id === id)

  function open(next: Editing) {
    editError = ''
    editing = next
  }

  // A folder can't move into itself or under one of its own subfolders (the
  // backend refuses it too); those aren't offered.
  function subtreeIds(id: number): Set<number> {
    const ids = new Set<number>([id])
    let grew = true
    while (grew) {
      grew = false
      for (const f of folders) {
        if (f.parent_folder_id !== null && ids.has(f.parent_folder_id) && !ids.has(f.id)) {
          ids.add(f.id)
          grew = true
        }
      }
    }
    return ids
  }
  $: moveOptions =
    editing?.kind === 'move'
      ? flatOptions.filter((opt) => !subtreeIds((editing as { id: number }).id).has(opt.folder.id))
      : flatOptions

  async function save() {
    if (!editing) return
    const name = 'name' in editing ? editing.name.trim() : ''
    if (editing.kind !== 'move' && !name) return
    saving = true
    editError = ''
    try {
      if (editing.kind === 'create') {
        if (selectedCustomerId === null) return
        const created = await api.post<Folder>('/folders', {
          name,
          customer_id: selectedCustomerId,
          parent_folder_id: editing.parentId,
        })
        folders = [...folders, created]
      } else if (editing.kind === 'rename') {
        const id = editing.id
        const updated = await api.patch<Folder>(`/folders/${id}`, { name })
        folders = folders.map((f) => (f.id === id ? updated : f))
      } else {
        const id = editing.id
        const updated = await api.patch<Folder>(`/folders/${id}`, { parent_folder_id: editing.parentId })
        folders = folders.map((f) => (f.id === id ? updated : f))
      }
      editing = null
    } catch (err) {
      editError = err instanceof ApiError ? err.detail : 'Failed to save the folder'
    } finally {
      saving = false
    }
  }

  async function deleteFolder(id: number) {
    const folder = folderById(id)
    if (!folder) return
    const ok = await confirmAction({
      title: 'Delete folder',
      message: `Delete the folder "${folder.name}"? A folder that still holds sources or subfolders can't be deleted.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
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

  const title = (e: Editing) =>
    e.kind === 'create' ? 'New folder' : e.kind === 'rename' ? 'Rename folder' : `Move "${e.name}"`
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Folders</h1>
    {#if !loading && customers.length > 0}
      <button class="btn btn-primary" on:click={() => open({ kind: 'create', name: '', parentId: null })}
        >+ New folder</button
      >
    {/if}
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
              {errorsById}
              on:rename={(e) => open({ kind: 'rename', id: e.detail.id, name: folderById(e.detail.id)?.name ?? '' })}
              on:move={(e) => {
                const folder = folderById(e.detail.id)
                if (folder) open({ kind: 'move', id: folder.id, name: folder.name, parentId: folder.parent_folder_id })
              }}
              on:addChild={(e) => open({ kind: 'create', name: '', parentId: e.detail.parentId })}
              on:delete={(e) => deleteFolder(e.detail.id)}
            />
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  {#if editing}
    <Modal title={title(editing)} on:close={() => (editing = null)}>
      <form id="folder-form" class="modal-form" on:submit|preventDefault={save}>
        {#if editing.kind !== 'move'}
          <label class="field">Name <input class="input" type="text" bind:value={editing.name} required /></label>
        {/if}
        {#if editing.kind !== 'rename'}
          <label class="field"
            >{editing.kind === 'move' ? 'Move under' : 'Inside'}
            <select class="input" bind:value={editing.parentId}>
              <option value={null}>(top level)</option>
              {#each moveOptions as opt (opt.folder.id)}
                <option value={opt.folder.id}>{'—'.repeat(opt.depth)} {opt.folder.name}</option>
              {/each}
            </select>
          </label>
        {/if}
        {#if editError}
          <p class="error" role="alert">{editError}</p>
        {/if}
      </form>
      <svelte:fragment slot="footer">
        <button class="btn btn-ghost" type="button" on:click={() => (editing = null)}>Cancel</button>
        <button class="btn btn-primary" type="submit" form="folder-form" disabled={saving}>
          {saving ? 'Saving…' : editing.kind === 'create' ? 'Create folder' : editing.kind === 'move' ? 'Move' : 'Save'}
        </button>
      </svelte:fragment>
    </Modal>
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
  .modal-form {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 1.5rem 0;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
  .hint {
    color: var(--text-faint);
  }
</style>
