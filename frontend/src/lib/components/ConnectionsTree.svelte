<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import type { ConnectionTreeNode } from '../connection-tree'
  import type { Source } from '../types'

  export let node: ConnectionTreeNode
  export let depth = 0
  export let expandedIds: Set<string>
  // Forces every node open regardless of `expandedIds` -- used while the
  // connections search box has a query, so a match several folders deep
  // isn't hidden behind a collapsed ancestor the viewer never manually
  // opened.
  export let forceExpand = false

  const dispatch = createEventDispatcher<{
    toggle: { id: string }
    open: { source: Source }
  }>()

  $: expanded = forceExpand || expandedIds.has(node.id)
</script>

<div class="node">
  <button class="row group" style="padding-left: {depth * 14}px" on:click={() => dispatch('toggle', { id: node.id })}>
    <span class="chevron">{expanded ? '▾' : '▸'}</span>
    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
    <span class="name">{node.name}</span>
  </button>

  {#if expanded}
    {#each node.children as child (child.id)}
      <svelte:self node={child} depth={depth + 1} {expandedIds} {forceExpand} on:toggle on:open />
    {/each}
    {#each node.sources as source (source.id)}
      <button
        class="row source"
        style="padding-left: {(depth + 1) * 14}px"
        on:click={() => dispatch('open', { source })}
      >
        <span class="chevron blank"></span>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5Z" />
          <path d="M14 2v4a2 2 0 0 0 2 2h4" />
        </svg>
        <span class="name">{source.name}</span>
        {#if source.is_system}
          <span class="badge badge-accent">system</span>
        {/if}
      </button>
    {/each}
  {/if}
</div>

<style>
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
  .chevron.blank {
    visibility: hidden;
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
  .badge-accent {
    flex: 0 0 auto;
    background: var(--accent-soft);
    color: var(--accent-hover);
    font-size: 0.68rem;
    font-weight: 600;
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
  }
</style>
