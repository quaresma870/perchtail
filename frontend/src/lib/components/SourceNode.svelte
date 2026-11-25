<script lang="ts">
  import { createEventDispatcher, getContext, onDestroy, onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../api'
  import {
    EXPLORER_MENUS,
    sourceExpandKeys,
    sourceMenu,
    type ExplorerMenu,
    type ExplorerMenus,
  } from '../context-menu'
  import { registerFolder } from '../explorer-refresh'
  import { advancedFilter, coveringScope, scopeContext, scopeKey, scopes, toggleScopeIn } from '../explorer-scope'
  import { sortEntries } from '../explorer-sort'
  import { explorerSort } from '../preferences'
  import { fetchListing, truncatedNotice } from '../listing'
  import { serverReachable } from '../server-status'
  import { sourceStatusView } from '../source-status'
  import type { BrowseEntry, Source } from '../types'
  import FolderTree from './FolderTree.svelte'
  import { explorerExpanded, setExpanded } from '../viewer-tabs'
  import { get } from 'svelte/store'

  /** A source (machine) in the viewer's explorer: expands in place to its
   * own directory tree, loaded lazily on first expand (#162). Errors stay
   * on this node -- one unreachable server doesn't break the explorer. */
  export let source: Source
  export let depth = 0
  export let activeKey: string | null = null
  export let initiallyExpanded = false

  const dispatch = createEventDispatcher<{
    open: { sourceId: number; sourceName: string; path: string; member: string | null; name: string; size?: number }
  }>()

  let expanded = false
  let loaded = false
  let loading = false
  let error = ''
  let entries: BrowseEntry[] = []
  // The listing was capped on the server (#347).
  let truncatedText = ''
  let element: HTMLElement

  $: canDownload = source.my_capabilities?.includes('download') ?? true

  async function load() {
    loading = true
    error = ''
    try {
      const listing = await fetchListing(`/sources/${source.id}/browse?path=`)
      entries = listing.entries
      truncatedText = listing.truncated ? truncatedNotice(listing) : ''
      loaded = true
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load'
    } finally {
      loading = false
    }
  }

  // Auto-refresh while open (#272): only the listing. New entries are
  // highlighted for a few seconds.
  let newPaths = new Set<string>()
  let newTimer: ReturnType<typeof setTimeout> | null = null
  let refreshing = false

  async function refresh(): Promise<boolean> {
    if (refreshing) return true
    refreshing = true
    try {
      // refresh=true: re-listing isn't "opening the source" again, so it
      // isn't audited as source.open.
      const listing = await fetchListing(`/sources/${source.id}/browse?path=&refresh=true`)
      const fresh = listing.entries
      truncatedText = listing.truncated ? truncatedNotice(listing) : ''
      const known = new Set(entries.map((e) => e.path))
      const added = fresh.filter((e) => !known.has(e.path)).map((e) => e.path)
      entries = fresh
      error = ''
      if (added.length > 0) {
        newPaths = new Set(added)
        if (newTimer) clearTimeout(newTimer)
        newTimer = setTimeout(() => (newPaths = new Set()), 5000)
      }
      refreshError = ''
      return true
    } catch {
      return false
    } finally {
      refreshing = false
    }
  }

  // Reachability (#276): the shared status, or what "Check now" just found.
  let checking = false
  let checked: { ok: boolean; detail: string; at: string } | null = null
  // A check is also recorded as the shared status, so once the next read of
  // the sources brings a newer status, that one is shown again.
  let checkedAgainst: string | null = null
  $: if (checked && source.status_checked_at !== checkedAgainst) checked = null
  $: view = sourceStatusView(source, checked, $serverReachable)

  async function checkNow() {
    checking = true
    try {
      const result = await api.post<{ ok: boolean; detail: string }>(`/sources/${source.id}/check`)
      checkedAgainst = source.status_checked_at
      checked = { ...result, at: new Date().toISOString() }
    } catch (err) {
      checkedAgainst = source.status_checked_at
      checked = {
        ok: false,
        detail: err instanceof ApiError ? err.detail : 'The check failed',
        at: new Date().toISOString(),
      }
    } finally {
      checking = false
    }
  }

  let refreshError = ''
  async function refreshNow() {
    refreshError = (await refresh()) ? '' : "Couldn't refresh this source"
  }

  // Advanced filter (#274): this source as a scope.
  $: ownScope = { sourceId: source.id, path: '' }
  $: inScope = $scopes.some((s) => scopeKey(s) === scopeKey(ownScope))
  // A ticked customer or folder of sources already includes it (#284).
  $: coveredBy = coveringScope($scopes, ownScope, $scopeContext)

  let unregister: (() => void) | null = null
  $: if (expanded && loaded && !unregister) {
    unregister = registerFolder({ sourceId: source.id, path: '', refresh })
  } else if (unregister && !(expanded && loaded)) {
    unregister()
    unregister = null
  }
  onDestroy(() => {
    unregister?.()
    if (newTimer) clearTimeout(newTimer)
  })

  async function toggle() {
    expanded = !expanded
    setExpanded(`source:${source.id}`, expanded)
    if (expanded && !loaded) await load()
    else if (expanded) refresh()
  }

  function retry() {
    load()
  }

  // Right-click menu (#287).
  const menus = getContext<ExplorerMenus | undefined>(EXPLORER_MENUS)
  let rowButton: HTMLElement
  $: caps = source.my_capabilities ?? []

  // Index file names now: what came of it, for a few seconds.
  let indexNote = ''
  let indexNoteTimer: ReturnType<typeof setTimeout> | null = null
  onDestroy(() => {
    if (indexNoteTimer) clearTimeout(indexNoteTimer)
  })

  async function indexNow() {
    let note: string
    try {
      const result = await api.post<{ started: boolean }>(`/sources/${source.id}/name-index`)
      note = result.started ? 'Listing the file names in the background.' : 'Already listing this source.'
    } catch (err) {
      note = err instanceof ApiError ? err.detail : 'Failed to start the listing'
    }
    indexNote = note
    if (indexNoteTimer) clearTimeout(indexNoteTimer)
    indexNoteTimer = setTimeout(() => (indexNote = ''), 5000)
  }

  /** Collapse all: the source and every folder in it close, and stay
   * closed when it opens again. */
  function collapseAll() {
    explorerExpanded.update((keys) => {
      const drop = new Set(sourceExpandKeys(keys, source.id))
      return drop.size === 0 ? keys : new Set([...keys].filter((key) => !drop.has(key)))
    })
    expanded = false
  }

  function buildMenu(): ExplorerMenu {
    return {
      label: 'Source actions',
      entries: sourceMenu({
        listed: expanded && loaded,
        expanded,
        system: source.is_system,
        canManageSource: caps.includes('manage_source'),
        canIndexNow: caps.includes('run_now') && source.name_index_enabled,
        canManageRules: caps.includes('manage_rules'),
      }),
      run: runMenu,
    }
  }

  function runMenu(id: string) {
    switch (id) {
      case 'refresh':
        return void refreshNow()
      case 'search-here':
        return menus?.searchHere({ sourceId: source.id, path: '' })
      case 'collapse-all':
        return collapseAll()
      case 'check-now':
        return void checkNow()
      case 'index-now':
        return void indexNow()
      case 'copy-host':
        return menus?.copy(source.host)
      case 'edit-source':
      case 'edit-rules':
        // The source's page holds its settings and its rules.
        return void push(`/settings/sources/${source.id}`)
    }
  }

  onMount(() => {
    if (initiallyExpanded) {
      toggle()
      element?.scrollIntoView({ block: 'nearest' })
    } else if (get(explorerExpanded).has(`source:${source.id}`)) {
      toggle()
    }
  })
</script>

<div class="source-node" bind:this={element}>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="source-row" on:contextmenu={(e) => menus?.mouse(e, rowButton, buildMenu)}>
  {#if $advancedFilter}
    <input
      type="checkbox"
      class="scope-check"
      checked={inScope || Boolean(coveredBy)}
      disabled={Boolean(coveredBy)}
      aria-label="Filter files in {source.name}"
      title={coveredBy ? 'Already included through a folder above' : 'Filter files in this source'}
      on:change={() => toggleScopeIn(ownScope)}
    />
  {/if}
  <button
    class="row source"
    class:expanded
    style="padding-left: {depth * 14}px"
    title={`${source.protocol} · ${source.host}`}
    bind:this={rowButton}
    on:click={toggle}
    on:keydown={(e) => menus?.key(e, buildMenu)}
  >
    <span class="chevron">{expanded ? '▾' : '▸'}</span>
    <span class="status-dot status-{view.state}" title={view.tooltip} aria-label={view.tooltip}></span>
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
  {#if source.my_capabilities.includes('manage_source')}
    <!-- The connection check needs manage sources on the source. -->
    <button
      class="refresh check"
      title="Check the connection now"
      aria-label="Check {source.name} now"
      disabled={checking}
      on:click={checkNow}>{checking ? '…' : '✓?'}</button
    >
  {/if}
  {#if expanded && loaded}
    <button class="refresh" title="Refresh this source" aria-label="Refresh {source.name}" on:click={refreshNow}>⟳</button>
  {/if}
  </div>

  {#if indexNote}
    <div class="hint" role="status" style="padding-left: {(depth + 1) * 14}px">{indexNote}</div>
  {/if}
  {#if expanded}
    <div class="tree-body">
      {#if refreshError}
        <div class="error" style="padding-left: {(depth + 1) * 14}px">{refreshError}</div>
      {/if}
      {#if loading}
        <div class="hint" style="padding-left: {(depth + 1) * 14}px">Loading…</div>
      {:else if error}
        <div class="error" style="padding-left: {(depth + 1) * 14}px">
          {error}
          <button class="link" on:click={retry}>Try again</button>
        </div>
      {:else}
        {#each sortEntries(entries, $explorerSort) as entry (entry.path)}
          <FolderTree
            sourceId={source.id}
            {entry}
            isNew={newPaths.has(entry.path)}
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
        {#if truncatedText}
          <div class="hint" role="status" style="padding-left: {(depth + 1) * 14}px">{truncatedText}</div>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<style>
  .status-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-faint);
  }
  .status-ok {
    background: var(--success);
  }
  .status-failing {
    background: var(--danger);
  }
  .status-disconnected {
    background: var(--warning);
  }
  .status-unknown {
    background: var(--text-faint);
    opacity: 0.6;
  }
  .check {
    font-size: 0.72rem;
  }
  .scope-check {
    flex: 0 0 auto;
    margin: 0 0 0 0.35rem;
    accent-color: var(--accent);
  }
  .source-row {
    display: flex;
    align-items: center;
  }
  .source-row .row {
    flex: 1;
    min-width: 0;
  }
  .refresh {
    flex: 0 0 auto;
    border: none;
    background: none;
    color: var(--text-faint);
    cursor: pointer;
    font-size: 0.9rem;
    padding: 0 0.5rem;
    visibility: hidden;
  }
  .source-row:hover .refresh {
    visibility: visible;
  }
  .refresh:hover {
    color: var(--accent-hover);
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
