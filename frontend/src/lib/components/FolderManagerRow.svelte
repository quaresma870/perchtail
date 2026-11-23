<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import type { FolderTreeNode } from '../folder-tree'
  import type { Source } from '../types'

  /** One folder in Settings -> Folders, with its sources and subfolders
   * below. Its actions are buttons; rename, move, subfolder and delete open
   * popups in the page (#301). Folders and sources can also be dragged onto
   * another folder (#355); the page decides where a drop is allowed. */
  export let node: FolderTreeNode
  export let depth = 0
  export let errorsById: Record<number, string>
  export let sourcesByFolder: Map<number | null, Source[]>
  export let canDropOn: (folderId: number | null) => boolean

  type Item = { kind: 'folder' | 'source'; id: number }
  const dispatch = createEventDispatcher<{
    rename: { id: number }
    move: { id: number }
    addChild: { parentId: number }
    delete: { id: number }
    dragItem: Item
    dragEnd: null
    dropOn: { folderId: number | null }
  }>()

  $: rowError = errorsById[node.folder.id] ?? ''
  $: sources = sourcesByFolder.get(node.folder.id) ?? []
  let over = false

  function start(event: DragEvent, item: Item) {
    event.stopPropagation()
    event.dataTransfer?.setData('text/plain', `${item.kind}:${item.id}`)
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
    dispatch('dragItem', item)
  }

  function dragOver(event: DragEvent) {
    if (!canDropOn(node.folder.id)) return
    event.preventDefault()
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
    over = true
  }

  function drop(event: DragEvent) {
    event.preventDefault()
    over = false
    if (canDropOn(node.folder.id)) dispatch('dropOn', { folderId: node.folder.id })
  }
</script>

<div
  class="row folder"
  class:over
  draggable="true"
  role="treeitem"
  aria-selected="false"
  tabindex="-1"
  on:dragstart={(e) => start(e, { kind: 'folder', id: node.folder.id })}
  on:dragend={() => dispatch('dragEnd', null)}
  on:dragover={dragOver}
  on:dragleave={() => (over = false)}
  on:drop={drop}
>
  <span class="indent" style="width: {depth * 18}px"></span>
  <span class="grip" title="Drag onto another folder to move it">⋮⋮</span>
  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </svg>
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
  <p class="row-error" style="padding-left: {depth * 18 + 18}px">{rowError}</p>
{/if}

{#each node.children as child (child.folder.id)}
  <svelte:self
    node={child}
    depth={depth + 1}
    {errorsById}
    {sourcesByFolder}
    {canDropOn}
    on:rename
    on:move
    on:addChild
    on:delete
    on:dragItem
    on:dragEnd
    on:dropOn
  />
{/each}

{#each sources as source (source.id)}
  <div
    class="row source"
    class:off={!source.enabled}
    draggable="true"
    role="treeitem"
    aria-selected="false"
    tabindex="-1"
    on:dragstart={(e) => start(e, { kind: 'source', id: source.id })}
    on:dragend={() => dispatch('dragEnd', null)}
  >
    <span class="indent" style="width: {(depth + 1) * 18}px"></span>
    <span class="grip" title="Drag onto a folder to move it">⋮⋮</span>
    <svg class="icon source-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
      <rect x="2" y="3" width="20" height="8" rx="2" />
      <rect x="2" y="13" width="20" height="8" rx="2" />
    </svg>
    <span class="source-name">{source.name}</span>
    <span class="host">{source.host}</span>
    {#if !source.enabled}<span class="badge">disabled</span>{/if}
  </div>
{/each}

<style>
  .row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.4rem;
    border-bottom: 1px solid var(--border-soft);
    border-radius: 6px;
    flex-wrap: wrap;
    cursor: grab;
  }
  .row.over {
    background: var(--accent-soft, rgba(99, 102, 241, 0.15));
    outline: 1px dashed var(--accent);
  }
  .indent {
    flex: 0 0 auto;
  }
  .grip {
    color: var(--text-faint);
    font-size: 0.75rem;
    letter-spacing: -2px;
    user-select: none;
  }
  .icon {
    flex: 0 0 auto;
    width: 15px;
    height: 15px;
    color: var(--accent);
  }
  .source-icon {
    color: var(--text-muted);
  }
  .name {
    font-weight: 600;
    color: var(--text);
    font-size: 0.88rem;
    margin-right: auto;
  }
  .source-name {
    color: var(--text);
    font-size: 0.85rem;
  }
  .host {
    color: var(--text-faint);
    font-size: 0.78rem;
    margin-right: auto;
  }
  .row.source.off .source-name {
    color: var(--text-faint);
  }
  .row-error {
    margin: 0.2rem 0 0.4rem;
    color: var(--danger);
    font-size: 0.8rem;
  }
</style>
