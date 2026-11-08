<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import FolderManagerRow from '../lib/components/FolderManagerRow.svelte'
  import Modal from '../lib/components/Modal.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { confirmAction } from '../lib/confirm'
  import { nestFoldersFlat, nestFoldersTree } from '../lib/folder-tree'
  import type { Customer, Folder, Source } from '../lib/types'

  let customers: Customer[] = []
  let selectedCustomerId: number | null = null
  let folders: Folder[] = []
  let loading = true
  let error = ''
  let errorsById: Record<number, string> = {}
  let sources: Source[] = []

  // Drag and drop (#355): a folder or a source dragged onto a folder, or
  // onto the customer's top level.
  type Item = { kind: 'folder' | 'source'; id: number }
  let dragged: Item | null = null
  let topOver = false
  let moveError = ''

  $: customerSources = sources
    .filter((s) => s.customer_id === selectedCustomerId)
    .sort((a, b) => a.name.localeCompare(b.name))
  $: sourcesByFolder = groupSources(customerSources)
  $: looseSources = sourcesByFolder.get(null) ?? []

  function groupSources(list: Source[]): Map<number | null, Source[]> {
    const map = new Map<number | null, Source[]>()
    for (const source of list) {
      const key = source.folder_id ?? null
      map.set(key, [...(map.get(key) ?? []), source])
    }
    return map
  }

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
      ;[customers, sources] = await Promise.all([
        api.get<Customer[]>('/customers'),
        api.get<Source[]>('/sources'),
      ])
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
  // Where the item being dragged may go: not where it already is, and a
  // folder never into itself or below itself.
  $: canDropOn = (folderId: number | null): boolean => {
    if (!dragged) return false
    if (dragged.kind === 'source') {
      const source = sources.find((s) => s.id === dragged?.id)
      return source !== undefined && (source.folder_id ?? null) !== folderId
    }
    const folder = folderById(dragged.id)
    if (!folder || folder.parent_folder_id === folderId) return false
    return folderId === null || !subtreeIds(folder.id).has(folderId)
  }

  async function dropOn(folderId: number | null) {
    const item = dragged
    dragged = null
    topOver = false
    if (!item) return
    moveError = ''
    try {
      if (item.kind === 'folder') {
        const updated = await api.patch<Folder>(`/folders/${item.id}`, { parent_folder_id: folderId })
        folders = folders.map((f) => (f.id === item.id ? updated : f))
      } else {
        const updated = await api.patch<Source>(`/sources/${item.id}`, { folder_id: folderId })
        sources = sources.map((s) => (s.id === item.id ? { ...s, folder_id: updated.folder_id } : s))
      }
    } catch (err) {
      moveError = err instanceof ApiError ? err.detail : 'The move failed'
    }
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

    <p class="hint drag-hint">Drag a folder or a source onto a folder to move it there, or onto the top bar to take it out of its folder.</p>
    {#if moveError}
      <p class="error" role="alert">{moveError}</p>
    {/if}

    <div class="card">
      <div
        class="top-level"
        class:active={dragged !== null && canDropOn(null)}
        class:over={topOver}
        role="group"
        aria-label="Top level of the customer"
        on:dragover={(e) => {
          if (!canDropOn(null)) return
          e.preventDefault()
          topOver = true
        }}
        on:dragleave={() => (topOver = false)}
        on:drop|preventDefault={() => canDropOn(null) && dropOn(null)}
      >
        Top level of {customers.find((c) => c.id === selectedCustomerId)?.name ?? 'this customer'}
      </div>
      {#if tree.length === 0 && looseSources.length === 0}
        <p class="empty">No folders or sources yet for this customer.</p>
      {:else}
        <div class="tree" role="tree">
          {#each tree as node (node.folder.id)}
            <FolderManagerRow
              {node}
              {errorsById}
              {sourcesByFolder}
              {canDropOn}
              on:dragItem={(e) => (dragged = e.detail)}
              on:dragEnd={() => {
                dragged = null
                topOver = false
              }}
              on:dropOn={(e) => dropOn(e.detail.folderId)}
              on:rename={(e) => open({ kind: 'rename', id: e.detail.id, name: folderById(e.detail.id)?.name ?? '' })}
              on:move={(e) => {
                const folder = folderById(e.detail.id)
                if (folder) open({ kind: 'move', id: folder.id, name: folder.name, parentId: folder.parent_folder_id })
              }}
              on:addChild={(e) => open({ kind: 'create', name: '', parentId: e.detail.parentId })}
              on:delete={(e) => deleteFolder(e.detail.id)}
            />
          {/each}
          {#each looseSources as source (source.id)}
            <div
              class="loose-source"
              class:off={!source.enabled}
              draggable="true"
              role="treeitem"
              aria-selected="false"
              tabindex="-1"
              on:dragstart={(e) => {
                e.dataTransfer?.setData('text/plain', `source:${source.id}`)
                dragged = { kind: 'source', id: source.id }
              }}
              on:dragend={() => {
                dragged = null
                topOver = false
              }}
            >
              <span class="grip">⋮⋮</span>
              <span class="loose-name">{source.name}</span>
              <span class="loose-host">{source.host}</span>
              {#if !source.enabled}<span class="badge">disabled</span>{/if}
              <span class="loose-note">not in a folder</span>
            </div>
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
  .drag-hint {
    font-size: 0.82rem;
    margin: -0.4rem 0 0.8rem;
  }
  .top-level {
    padding: 0.45rem 0.6rem;
    margin-bottom: 0.5rem;
    border: 1px dashed var(--border-soft);
    border-radius: 6px;
    color: var(--text-faint);
    font-size: 0.82rem;
  }
  .top-level.active {
    border-color: var(--accent);
    color: var(--text-muted);
  }
  .top-level.over {
    background: var(--accent-soft, rgba(99, 102, 241, 0.15));
  }
  .loose-source {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.4rem;
    border-bottom: 1px solid var(--border-soft);
    cursor: grab;
    font-size: 0.85rem;
  }
  .loose-source .grip {
    color: var(--text-faint);
    font-size: 0.75rem;
    letter-spacing: -2px;
  }
  .loose-name {
    color: var(--text);
  }
  .loose-source.off .loose-name {
    color: var(--text-faint);
  }
  .loose-host {
    color: var(--text-faint);
    font-size: 0.78rem;
    margin-right: auto;
  }
  .loose-note {
    color: var(--text-faint);
    font-size: 0.75rem;
    font-style: italic;
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
