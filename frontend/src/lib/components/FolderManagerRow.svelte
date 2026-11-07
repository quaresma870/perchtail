<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import type { FolderTreeNode } from '../folder-tree'

  /** One folder in Settings -> Folders, with its subfolders below. Its
   * actions are buttons; rename, move, subfolder and delete open popups in
   * the page (#301). */
  export let node: FolderTreeNode
  export let depth = 0
  export let errorsById: Record<number, string>

  const dispatch = createEventDispatcher<{
    rename: { id: number }
    move: { id: number }
    addChild: { parentId: number }
    delete: { id: number }
  }>()

  $: rowError = errorsById[node.folder.id] ?? ''
</script>

<div class="row">
  <span class="indent" style="width: {depth * 16}px"></span>
  <span class="name">{node.folder.name}</span>
  <div class="row-actions">
    <button class="btn btn-ghost btn-sm" on:click={() => dispatch('rename', { id: node.folder.id })}>Rename</button>
    <button class="btn btn-ghost btn-sm" on:click={() => dispatch('move', { id: node.folder.id })}>Move</button>
    <button class="btn btn-ghost btn-sm" on:click={() => dispatch('addChild', { parentId: node.folder.id })}
      >+ Subfolder</button
    >
    <button class="btn btn-danger btn-sm" on:click={() => dispatch('delete', { id: node.folder.id })}>Delete</button>
  </div>
</div>
{#if rowError}
  <p class="row-error" style="padding-left: {depth * 16 + 16}px">{rowError}</p>
{/if}

{#each node.children as child (child.folder.id)}
  <svelte:self node={child} depth={depth + 1} {errorsById} on:rename on:move on:addChild on:delete />
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
  .row-error {
    margin: 0.2rem 0 0.4rem;
    color: var(--danger);
    font-size: 0.8rem;
  }
</style>
