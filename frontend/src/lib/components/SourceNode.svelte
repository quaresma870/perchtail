<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import type { BrowseEntry, Source } from '../types'
  import FolderTree from './FolderTree.svelte'

  /** A source (machine) in the viewer's explorer: expands in place to its
   * own directory tree, loaded lazily on first expand (#162). Errors stay
   * on this node -- one unreachable server doesn't break the explorer. */
  export let source: Source
  export let depth = 0
  export let activeKey: string | null = null
  export let initiallyExpanded = false

  const dispatch = createEventDispatcher<{
    open: { sourceId: number; sourceName: string; path: string; member: string | null; name: string }
  }>()

  let expanded = false
  let loaded = false
  let loading = false
  let error = ''
  let entries: BrowseEntry[] = []
  let element: HTMLElement

  $: canDownload = source.my_capabilities?.includes('download') ?? true

  async function load() {
    loading = true
    error = ''
    try {
      entries = await api.get<BrowseEntry[]>(`/sources/${source.id}/browse?path=`)
      loaded = true
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load'
    } finally {
      loading = false
    }
  }

  async function toggle() {
    expanded = !expanded
    if (expanded && !loaded) await load()
  }

  function retry() {
    load()
  }

  onMount(() => {
    if (initiallyExpanded) {
      toggle()
      element?.scrollIntoView({ block: 'nearest' })
    }
  })
</script>

<div class="source-node" bind:this={element}>
  <button
    class="row source"
    class:expanded
    style="padding-left: {depth * 14}px"
    title={`${source.protocol} · ${source.host}`}
    on:click={toggle}
  >
    <span class="chevron">{expanded ? '▾' : '▸'}</span>
    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
      <rect x="2" y="3" width="20" height="8" rx="2" />
      <rect x="2" y="13" width="20" height="8" rx="2" />
      <path d="M6 7h.01M6 17h.01" />
    </svg>
    <span class="name">{source.name}</span>
    {#if source.is_system}
      <span class="badge badge-accent">system</span>
    {/if}
  </button>

  {#if expanded}
    <div class="tree-body">
      {#if loading}
        <div class="hint" style="padding-left: {(depth + 1) * 14}px">Loading…</div>
      {:else if error}
        <div class="error" style="padding-left: {(depth + 1) * 14}px">
          {error}
          <button class="link" on:click={retry}>Try again</button>
        </div>
      {:else}
        {#each entries as entry (entry.path)}
          <FolderTree
            sourceId={source.id}
            {entry}
            depth={depth + 1}
            {activeKey}
            {canDownload}
            on:open={(e) => dispatch('open', { sourceId: source.id, sourceName: source.name, ...e.detail })}
          />
        {/each}
        {#if entries.length === 0}
          <div class="hint" style="padding-left: {(depth + 1) * 14}px">
            Nothing visible here — check the source's rules.
          </div>
        {/if}
      {/if}
    </div>
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
    font-size: 0.84rem;
    color: var(--text);
    border-radius: var(--radius-sm);
  }
  .row:hover {
    background: var(--bg-hover);
  }
  .chevron {
    flex: 0 0 auto;
    width: 0.8rem;
    color: var(--text-faint);
    font-size: 0.7rem;
  }
  .icon {
    flex: 0 0 auto;
    width: 15px;
    height: 15px;
    color: var(--accent);
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .hint,
  .error {
    font-size: 0.8rem;
    padding: 0.25rem 0.5rem;
  }
  .hint {
    color: var(--text-faint);
  }
  .error {
    color: var(--danger, #f87171);
  }
  .link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-size: 0.8rem;
    padding: 0 0 0 0.4rem;
  }
</style>
