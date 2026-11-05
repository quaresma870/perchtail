<script lang="ts">
  // Narrows a search to customers, folders (with everything below them)
  // and single sources, plus a path filter (#163). Built from the same
  // RBAC-filtered source list the Viewer uses, so it only ever offers what
  // the user can already see.
  import { createEventDispatcher } from 'svelte'
  import { buildConnectionTree, type ConnectionTreeNode } from '../connection-tree'
  import { scopeKey, toggleScopeItem, type ScopeItem, type SearchScope } from '../search-scope'
  import type { Source } from '../types'

  export let sources: Source[]
  export let scope: SearchScope

  const dispatch = createEventDispatcher<{ change: SearchScope }>()

  let open = false
  let filter = ''

  $: tree = buildConnectionTree(sources)
  $: selected = new Set(scope.items.map(scopeKey))
  $: needle = filter.trim().toLowerCase()

  function nodeItem(node: ConnectionTreeNode): ScopeItem | null {
    const match = /^(customer|folder):(\d+)$/.exec(node.id)
    return match ? { kind: match[1] as 'customer' | 'folder', id: Number(match[2]), name: node.name } : null
  }

  function toggle(item: ScopeItem) {
    dispatch('change', { ...scope, items: toggleScopeItem(scope.items, item) })
  }

  function setPath(event: Event) {
    dispatch('change', { ...scope, path: (event.target as HTMLInputElement).value })
  }

  function nodeMatches(node: ConnectionTreeNode): boolean {
    if (!needle) return true
    return (
      node.name.toLowerCase().includes(needle) ||
      node.sources.some((s) => s.name.toLowerCase().includes(needle)) ||
      node.children.some(nodeMatches)
    )
  }

  const kindLabel = { customer: 'Customer', folder: 'Folder', source: 'Source' }
</script>

<div class="scope">
  <div class="chips">
    {#if scope.items.length === 0}
      <span class="hint">All sources you can view</span>
    {/if}
    {#each scope.items as item (scopeKey(item))}
      <span class="chip">
        <span class="kind">{kindLabel[item.kind]}</span>
        {item.name}
        <button
          type="button"
          class="remove"
          aria-label={`Remove ${item.name} from the search scope`}
          on:click={() => toggle(item)}>✕</button
        >
      </span>
    {/each}
    <button
      type="button"
      class="btn btn-ghost small"
      aria-expanded={open}
      on:click={() => (open = !open)}>{open ? 'Done' : 'Narrow…'}</button
    >
    {#if scope.items.length > 0}
      <button
        type="button"
        class="btn btn-ghost small"
        on:click={() => dispatch('change', { ...scope, items: [] })}>Clear</button
      >
    {/if}
  </div>

  <label class="path">
    <span>Path contains</span>
    <input
      class="input mono"
      value={scope.path}
      on:change={setPath}
      placeholder="e.g. iis/ or app/*.log"
      spellcheck="false"
    />
  </label>

  {#if open}
    <div class="panel">
      <input
        class="input"
        type="search"
        placeholder="Filter customers, folders and sources…"
        bind:value={filter}
      />
      <ul class="tree" role="tree" aria-label="Search scope">
        {#each tree as node (node.id)}
          {#if nodeMatches(node)}
            {@render treeNode(node, 0)}
          {/if}
        {/each}
      </ul>
    </div>
  {/if}
</div>

{#snippet treeNode(node: ConnectionTreeNode, depth: number)}
  {@const item = nodeItem(node)}
  <li role="treeitem" aria-selected={item ? selected.has(scopeKey(item)) : false}>
    <label class="row" style={`padding-left: ${depth * 1.1}rem`}>
      {#if item}
        <input type="checkbox" checked={selected.has(scopeKey(item))} on:change={() => toggle(item)} />
      {:else}
        <span class="spacer"></span>
      {/if}
      <span class="group">{node.name}</span>
    </label>
    <ul role="group">
      {#each node.children as child (child.id)}
        {#if nodeMatches(child)}
          {@render treeNode(child, depth + 1)}
        {/if}
      {/each}
      {#each node.sources as source (source.id)}
        {#if !needle || source.name.toLowerCase().includes(needle) || node.name.toLowerCase().includes(needle)}
          {@const sourceItem = { kind: 'source' as const, id: source.id, name: source.name }}
          <li role="treeitem" aria-selected={selected.has(scopeKey(sourceItem))}>
            <label class="row" style={`padding-left: ${(depth + 1) * 1.1}rem`}>
              <input
                type="checkbox"
                checked={selected.has(scopeKey(sourceItem))}
                on:change={() => toggle(sourceItem)}
              />
              {source.name}
              {#if !source.search_indexing_enabled}
                <span class="muted">(not indexed)</span>
              {/if}
            </label>
          </li>
        {/if}
      {/each}
    </ul>
  </li>
{/snippet}

<style>
  .scope {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: center;
  }
  .chip {
    display: inline-flex;
    flex-direction: row;
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.3rem 0.15rem 0.55rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-elevated-2);
    font-size: 0.8rem;
    color: var(--text);
  }
  .kind {
    color: var(--text-faint);
    font-size: 0.72rem;
  }
  .remove {
    border: none;
    background: none;
    color: var(--text-faint);
    cursor: pointer;
    padding: 0 0.2rem;
  }
  .path {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .path span {
    white-space: nowrap;
  }
  .path input {
    flex: 1;
  }
  .panel {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .tree,
  .tree ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .tree {
    max-height: 300px;
    overflow: auto;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.2rem 0.3rem;
    font-size: 0.84rem;
    color: var(--text);
    cursor: pointer;
  }
  .group {
    font-weight: 600;
  }
  .spacer {
    width: 13px;
  }
  .muted,
  .hint {
    color: var(--text-faint);
    font-size: 0.8rem;
  }
  .mono {
    font-family: var(--font-mono);
  }
  .small {
    padding: 0.25rem 0.55rem;
    font-size: 0.8rem;
  }
</style>
