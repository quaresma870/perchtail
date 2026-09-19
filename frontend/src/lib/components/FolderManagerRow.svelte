<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import type { FlatFolderOption, FolderTreeNode } from '../folder-tree'

  export let node: FolderTreeNode
  export let depth = 0
  export let flatOptions: FlatFolderOption[]
  export let renamingId: number | null
  export let renameValue: string
  export let errorsById: Record<number, string>

  const dispatch = createEventDispatcher<{
    startRename: { id: number }
    renameInput: { value: string }
    submitRename: { id: number }
    cancelRename: void
    move: { id: number; parentId: number | null }
    addChild: { parentId: number }
    delete: { id: number }
  }>()

  $: isRenaming = renamingId === node.folder.id
  $: rowError = errorsById[node.folder.id] ?? ''

  // A folder can't become its own parent, nor its own descendant's parent
  // (the backend rejects that too -- see folders.py's `_validate_parent`).
  // Filtered here purely so the picker doesn't even offer an obviously
  // invalid choice; the backend's own check is still the source of truth,
  // so a race (someone else moved a folder concurrently) still surfaces
  // as a normal `rowError` from the rejected request, not a client crash.
  function isSelfOrDescendant(candidateId: number, subtree: FolderTreeNode): boolean {
    if (candidateId === subtree.folder.id) return true
    return subtree.children.some((child) => isSelfOrDescendant(candidateId, child))
  }
  $: moveOptions = flatOptions.filter((opt) => !isSelfOrDescendant(opt.folder.id, node))
</script>

<div class="row">
  <span class="indent" style="width: {depth * 16}px"></span>
  {#if isRenaming}
    <form
      class="rename-form"
      on:submit|preventDefault={() => dispatch('submitRename', { id: node.folder.id })}
    >
      <input
        class="input"
        type="text"
        value={renameValue}
        on:input={(e) => dispatch('renameInput', { value: (e.target as HTMLInputElement).value })}
        on:keydown={(e) => e.key === 'Escape' && dispatch('cancelRename')}
      />
      <button class="link" type="submit">Save</button>
      <button class="link" type="button" on:click={() => dispatch('cancelRename')}>Cancel</button>
    </form>
  {:else}
    <span class="name">{node.folder.name}</span>
    <div class="row-actions">
      <button class="link" on:click={() => dispatch('startRename', { id: node.folder.id })}>
        Rename
      </button>
      <button class="link" on:click={() => dispatch('addChild', { parentId: node.folder.id })}>
        + Subfolder
      </button>
      <select
        class="input move-select"
        value={node.folder.parent_folder_id ?? ''}
        on:change={(e) => {
          const value = (e.target as HTMLSelectElement).value
          dispatch('move', { id: node.folder.id, parentId: value === '' ? null : Number(value) })
        }}
      >
        <option value="">(top level)</option>
        {#each moveOptions as opt (opt.folder.id)}
          <option value={opt.folder.id}>{'—'.repeat(opt.depth)} {opt.folder.name}</option>
        {/each}
      </select>
      <button class="link danger" on:click={() => dispatch('delete', { id: node.folder.id })}>
        Delete
      </button>
    </div>
  {/if}
</div>
{#if rowError}
  <p class="row-error" style="padding-left: {depth * 16 + 16}px">{rowError}</p>
{/if}

{#each node.children as child (child.folder.id)}
  <svelte:self
    node={child}
    depth={depth + 1}
    {flatOptions}
    {renamingId}
    {renameValue}
    {errorsById}
    on:startRename
    on:renameInput
    on:submitRename
    on:cancelRename
    on:move
    on:addChild
    on:delete
  />
{/each}

<style>
  .row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--border-soft);
    flex-wrap: wrap;
  }
  .indent {
    flex: 0 0 auto;
  }
  .name {
    font-weight: 600;
    color: var(--text);
    font-size: 0.88rem;
    margin-right: auto;
  }
  .row-actions {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    flex-wrap: wrap;
  }
  .move-select {
    width: auto;
    max-width: 220px;
    font-size: 0.78rem;
    padding: 0.25rem 0.5rem;
  }
  .rename-form {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1;
  }
  .rename-form .input {
    max-width: 260px;
  }
  button.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    padding: 0;
    font-size: 0.82rem;
    white-space: nowrap;
  }
  button.link.danger {
    color: var(--danger);
  }
  .row-error {
    color: var(--danger);
    font-size: 0.78rem;
    margin: 0.15rem 0 0.4rem;
  }
</style>
