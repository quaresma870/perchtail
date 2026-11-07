<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import type { ConnectionTreeNode } from '../connection-tree'
  import { advancedFilter, coveringScope, scopeContext, scopeKey, scopes, toggleScopeIn } from '../explorer-scope'
  import SourceNode from './SourceNode.svelte'

  export let node: ConnectionTreeNode
  export let depth = 0
  export let expandedIds: Set<string>
  // Forces every node open regardless of `expandedIds` -- used while the
  // connections search box has a query, so a match several folders deep
  // isn't hidden behind a collapsed ancestor the viewer never manually
  // opened.
  export let forceExpand = false
  export let activeKey: string | null = null
  // Source to expand on mount (deep link /viewer/:sourceId).
  export let focusSourceId: number | null = null

  const dispatch = createEventDispatcher<{
    toggle: { id: string }
  }>()

  $: expanded = forceExpand || expandedIds.has(node.id)

  // Advanced filter (#284): this customer or folder as a scope, i.e. every
  // source under it.
  $: ownScope = { groupId: node.id, name: node.name }
  $: inScope = $scopes.some((s) => scopeKey(s) === scopeKey(ownScope))
  $: coveredBy = coveringScope($scopes, ownScope, $scopeContext)
</script>

<div class="node">
  <div class="group-row">
  {#if $advancedFilter}
    <input
      type="checkbox"
      class="scope-check"
      checked={inScope || Boolean(coveredBy)}
      disabled={Boolean(coveredBy)}
      aria-label="Filter files in all sources in {node.name}"
      title={coveredBy ? 'Already included through a folder above' : 'Filter files in every source in here'}
      on:change={() => toggleScopeIn(ownScope)}
    />
  {/if}
  <button class="row group" style="padding-left: {depth * 14}px" on:click={() => dispatch('toggle', { id: node.id })}>
    <span class="chevron">{expanded ? '▾' : '▸'}</span>
    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
    <span class="name">{node.name}</span>
  </button>
  </div>

  {#if expanded}
    {#each node.children as child (child.id)}
      <svelte:self
        node={child}
        depth={depth + 1}
        {expandedIds}
        {forceExpand}
        {activeKey}
        {focusSourceId}
        on:toggle
        on:open
      />
    {/each}
    {#each node.sources as source (source.id)}
      <SourceNode
        {source}
        depth={depth + 1}
        {activeKey}
        initiallyExpanded={focusSourceId === source.id}
        on:open
      />
    {/each}
  {/if}
</div>

<style>
  .group-row {
    display: flex;
    align-items: center;
  }
  .group-row .row {
    flex: 1;
    min-width: 0;
  }
  .scope-check {
    flex: 0 0 auto;
    margin: 0 0 0 0.35rem;
    accent-color: var(--accent);
  }
  .row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    text-align: left;
    border: none;
    background: none;
    padding: 0.32rem 0.5rem;
    cursor: pointer;
    font-size: 0.83rem;
    color: var(--text-muted);
    border-radius: var(--radius-sm);
  }
  .row:hover {
    background: var(--bg-hover);
  }
  .row.group {
    color: var(--text);
    font-weight: 600;
  }
  .chevron {
    flex: 0 0 auto;
    width: 0.8rem;
    display: inline-block;
    color: var(--text-faint);
    font-size: 0.7rem;
  }
  .icon {
    flex: 0 0 auto;
    width: 14px;
    height: 14px;
    color: var(--text-faint);
  }
  .row.group .icon {
    color: #7c93c9;
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
