<script lang="ts">
  // One scope of the advanced filter (#274): the files of a source or
  // folder whose names match the filter, newest first or however the user
  // sorts. Lists the scope's own folder; "search subfolders too" walks the
  // folders below on demand, within limits, so a whole source is never
  // walked unasked. Every listing is the live browse, under the rules.
  import { createEventDispatcher, onDestroy, onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import { registerFolder } from '../explorer-refresh'
  import { removeScope, nameMatches, type Scope } from '../explorer-scope'
  import { formatEntryTime, sortEntries } from '../explorer-sort'
  import { explorerSort } from '../preferences'
  import type { BrowseEntry } from '../types'

  export let scope: Scope
  export let sourceName: string
  export let query = ''

  // How far "search subfolders too" goes.
  const MAX_DEPTH = 3
  const MAX_FOLDERS = 50

  const dispatch = createEventDispatcher<{
    open: { sourceId: number; sourceName: string; path: string; member: null; name: string }
  }>()

  // Folder path -> its entries, for every folder listed so far.
  let listings = new Map<string, BrowseEntry[]>()
  let loading = false
  let walking = false
  let walked = false
  let truncated = false
  let error = ''

  const browse = (path: string, refresh = false) =>
    api.get<BrowseEntry[]>(
      `/sources/${scope.sourceId}/browse?path=${encodeURIComponent(path)}${refresh ? '&refresh=true' : ''}`,
    )

  async function load() {
    loading = true
    error = ''
    try {
      listings = new Map([[scope.path, await browse(scope.path)]])
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load'
    } finally {
      loading = false
    }
  }

  /** Lists the folders below the scope, breadth first, up to the limits. */
  async function searchSubfolders() {
    walking = true
    error = ''
    try {
      const queue: { path: string; depth: number }[] = []
      const enqueue = (path: string, depth: number) => {
        for (const entry of listings.get(path) ?? []) {
          if (entry.is_dir && !entry.is_archive) queue.push({ path: entry.path, depth })
        }
      }
      enqueue(scope.path, 1)
      let listed = 0
      truncated = false
      while (queue.length > 0) {
        const next = queue.shift()!
        if (listings.has(next.path)) continue
        if (listed >= MAX_FOLDERS || next.depth > MAX_DEPTH) {
          truncated = true
          break
        }
        try {
          listings.set(next.path, await browse(next.path))
          listings = listings
        } catch {
          // a folder that can't be listed is skipped, the rest go on
        }
        listed += 1
        enqueue(next.path, next.depth + 1)
      }
      walked = true
    } finally {
      walking = false
    }
  }

  // Auto-refresh (#272): every folder listed so far is listed again.
  async function refresh(): Promise<boolean> {
    try {
      const next = new Map<string, BrowseEntry[]>()
      for (const path of listings.keys()) {
        try {
          next.set(path, await browse(path, path === scope.path))
        } catch (err) {
          if (path === scope.path) throw err
        }
      }
      listings = next
      return true
    } catch {
      return false
    }
  }

  let unregister: (() => void) | null = null
  onMount(() => {
    load()
    unregister = registerFolder({ sourceId: scope.sourceId, path: scope.path, refresh })
  })
  onDestroy(() => unregister?.())

  const relative = (path: string) =>
    scope.path && path.startsWith(`${scope.path}/`) ? path.slice(scope.path.length + 1) : path

  $: files = [...listings.values()].flat().filter((e) => !e.is_dir && !e.is_archive)
  $: matches = sortEntries(
    files.filter((e) => nameMatches(e.name, query)),
    $explorerSort,
  )
</script>

<div class="scope">
  <div class="scope-head">
    <span class="scope-name" title={scope.path ? `${sourceName} › ${scope.path}` : sourceName}>
      {sourceName}{scope.path ? ` › ${scope.path}` : ''}
    </span>
    <span class="count">{matches.length} of {files.length}</span>
    <button
      class="link"
      title="Remove this source or folder from the filter"
      aria-label="Remove {sourceName}{scope.path ? ` › ${scope.path}` : ''} from the filter"
      on:click={() => removeScope(scope)}>✕</button
    >
  </div>
  {#if loading}
    <div class="hint">Loading…</div>
  {:else if error}
    <div class="error">{error}</div>
  {:else}
    {#each matches as entry (entry.path)}
      <button
        class="file"
        aria-label={relative(entry.path)}
        on:click={() =>
          dispatch('open', {
            sourceId: scope.sourceId,
            sourceName,
            path: entry.path,
            member: null,
            name: entry.name,
          })}
      >
        <span class="path">{relative(entry.path)}</span>
        {#if entry.mtime}
          <span class="time" title={new Date(entry.mtime * 1000).toLocaleString()}
            >{formatEntryTime(entry.mtime)}</span
          >
        {/if}
      </button>
    {/each}
    {#if matches.length === 0}
      <div class="hint">{files.length === 0 ? 'No files here.' : 'No file names match.'}</div>
    {/if}
    {#if !walked}
      <button class="link more" disabled={walking} on:click={searchSubfolders}>
        {walking ? 'Searching subfolders…' : 'Search subfolders too'}
      </button>
    {:else if truncated}
      <div class="hint">
        Searched {listings.size - 1} subfolders, up to {MAX_DEPTH} levels down; narrow the scope to
        go further.
      </div>
    {/if}
  {/if}
</div>

<style>
  .scope {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    padding: 0.35rem 0.25rem 0.6rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .scope-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8rem;
    color: var(--text);
    font-weight: 600;
    padding: 0.15rem 0.4rem;
  }
  .scope-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .count {
    font-weight: 400;
    font-size: 0.72rem;
    color: var(--text-faint);
  }
  .file {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border: none;
    background: none;
    text-align: left;
    padding: 0.22rem 0.4rem 0.22rem 1rem;
    font-size: 0.82rem;
    color: var(--text-muted);
    cursor: pointer;
    border-radius: var(--radius-sm);
  }
  .file:hover {
    background: var(--bg-hover);
    color: var(--text);
  }
  .path {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .time {
    flex: 0 0 auto;
    font-size: 0.72rem;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
  }
  .more {
    align-self: flex-start;
    margin: 0.2rem 0 0 1rem;
    font-size: 0.78rem;
  }
  .hint,
  .error {
    font-size: 0.78rem;
    color: var(--text-faint);
    padding: 0.15rem 1rem;
  }
  .error {
    color: var(--danger);
  }
</style>
