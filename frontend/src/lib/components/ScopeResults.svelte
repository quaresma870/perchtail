<script lang="ts">
  // One source or folder of the advanced filter (#274, #284): the files
  // whose names match the filter, in the explorer's order. The folders
  // below are searched right away, breadth first, within limits (depth,
  // folders, time) and through the shared listing limiter, with a Stop
  // button. Every listing is the live browse, under the rules.
  import { createEventDispatcher, onDestroy, onMount } from 'svelte'
  import { get } from 'svelte/store'
  import { api, ApiError } from '../api'
  import { registerFolder } from '../explorer-refresh'
  import { removeScope, stopSearches, type Matcher, type PathScope } from '../explorer-scope'
  import { formatEntryTime, sortEntries } from '../explorer-sort'
  import { listingLimiter, PER_SOURCE } from '../listing-limiter'
  import { explorerSort } from '../preferences'
  import type { BrowseEntry } from '../types'

  export let scope: PathScope
  export let sourceName: string
  /** The compiled filter; null while the filter is invalid (no results). */
  export let matcher: Matcher | null
  /** Part of a ticked customer or folder of sources: no ✕, and hidden once
   * searched if nothing matches. */
  export let partOfGroup = false

  // How far the search goes, per source or folder ticked.
  const MAX_DEPTH = 6
  const MAX_FOLDERS = 300
  const TIME_BUDGET_MS = 90_000
  // Auto-refresh lists again the ticked folder and the folders that had
  // files, not every folder searched.
  const REFRESH_MAX_FOLDERS = 50

  const dispatch = createEventDispatcher<{
    open: { sourceId: number; sourceName: string; path: string; member: null; name: string }
  }>()

  // Folder path -> its entries, for every folder listed so far.
  let listings = new Map<string, BrowseEntry[]>()
  let state: 'searching' | 'done' | 'limit' | 'stopped' = 'searching'
  let error = ''
  let stopped = false
  let destroyed = false

  const browse = (path: string, refresh = false) =>
    api.get<BrowseEntry[]>(
      `/sources/${scope.sourceId}/browse?path=${encodeURIComponent(path)}${refresh ? '&refresh=true' : ''}`,
    )

  async function search() {
    const started = Date.now()
    const queue: { path: string; depth: number }[] = [{ path: scope.path, depth: 0 }]
    const inFlight = new Set<Promise<void>>()
    let hitLimit = false

    const listOne = async (item: { path: string; depth: number }) => {
      try {
        const entries = await listingLimiter.run(scope.sourceId, () =>
          stopped || destroyed ? Promise.resolve(null) : browse(item.path),
        )
        if (!entries || destroyed) return
        listings.set(item.path, entries)
        listings = listings
        for (const entry of entries) {
          if (!entry.is_dir || entry.is_archive) continue
          if (item.depth < MAX_DEPTH) queue.push({ path: entry.path, depth: item.depth + 1 })
          else hitLimit = true
        }
      } catch (err) {
        // The ticked folder itself failing is an error; a folder below it
        // that can't be listed is skipped, the rest go on.
        if (item.path === scope.path) error = err instanceof ApiError ? err.detail : 'Failed to load'
      }
    }

    while ((queue.length > 0 || inFlight.size > 0) && !stopped && !destroyed) {
      while (queue.length > 0 && inFlight.size < PER_SOURCE && !stopped) {
        if (listings.size + inFlight.size >= MAX_FOLDERS || Date.now() - started > TIME_BUDGET_MS) {
          hitLimit = true
          queue.length = 0
          break
        }
        const task: Promise<void> = listOne(queue.shift()!).finally(() => inFlight.delete(task))
        inFlight.add(task)
      }
      if (inFlight.size > 0) await Promise.race(inFlight)
    }
    state = stopped ? 'stopped' : hitLimit ? 'limit' : 'done'
  }

  function stop() {
    if (state === 'searching') stopped = true
  }

  // "Stop" in the filter bar stops every search.
  let stopSeen = get(stopSearches)
  $: if ($stopSearches !== stopSeen) {
    stopSeen = $stopSearches
    stop()
  }

  // Auto-refresh (#272): the ticked folder and the folders that had files.
  async function refresh(): Promise<boolean> {
    if (state === 'searching') return true
    const paths = [
      scope.path,
      ...[...listings.entries()]
        .filter(([path, entries]) => path !== scope.path && entries.some((e) => !e.is_dir))
        .map(([path]) => path),
    ].slice(0, REFRESH_MAX_FOLDERS)
    try {
      const results = await Promise.all(
        paths.map((path) =>
          listingLimiter
            .run(scope.sourceId, () => browse(path, path === scope.path))
            .then((entries) => [path, entries] as const)
            .catch((err) => {
              if (path === scope.path) throw err
              return [path, null] as const
            }),
        ),
      )
      for (const [path, entries] of results) {
        if (entries) listings.set(path, entries)
        else listings.delete(path)
      }
      listings = listings
      return true
    } catch {
      return false
    }
  }

  let unregister: (() => void) | null = null
  onMount(() => {
    search()
    unregister = registerFolder({ sourceId: scope.sourceId, path: scope.path, refresh })
  })
  onDestroy(() => {
    destroyed = true
    unregister?.()
  })

  const relative = (path: string) =>
    scope.path && path.startsWith(`${scope.path}/`) ? path.slice(scope.path.length + 1) : path

  $: files = [...listings.values()].flat().filter((e) => !e.is_dir && !e.is_archive)
  $: matches = matcher
    ? sortEntries(
        files.filter((e) => matcher!(e.name, relative(e.path))),
        $explorerSort,
      )
    : []
  $: label = scope.path ? `${sourceName} › ${scope.path}` : sourceName
  $: hidden = partOfGroup && state !== 'searching' && !error && matches.length === 0
</script>

{#if !hidden}
  <div class="scope" class:member={partOfGroup}>
    <div class="scope-head">
      <span class="scope-name" title={label}>{label}</span>
      <span class="count">{matches.length} of {files.length}</span>
      {#if state === 'searching'}
        <button class="link stop" title="Stop searching the folders below" on:click={stop}>Stop</button>
      {/if}
      {#if !partOfGroup}
        <button
          class="link"
          title="Remove this source or folder from the filter"
          aria-label="Remove {label} from the filter"
          on:click={() => removeScope(scope)}>✕</button
        >
      {/if}
    </div>
    {#if error}
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
      {#if state === 'searching'}
        <div class="hint">Searching… {listings.size} {listings.size === 1 ? 'folder' : 'folders'} so far</div>
      {:else if matches.length === 0 && matcher}
        <div class="hint">{files.length === 0 ? 'No files here.' : 'No file names match.'}</div>
      {/if}
      {#if state === 'limit'}
        <div class="hint">
          Stopped at the limit ({MAX_DEPTH} levels, {MAX_FOLDERS} folders or {TIME_BUDGET_MS / 1000} s) after
          {listings.size} folders: tick a narrower folder to go further.
        </div>
      {:else if state === 'stopped'}
        <div class="hint">Stopped after {listings.size} {listings.size === 1 ? 'folder' : 'folders'}.</div>
      {/if}
    {/if}
  </div>
{/if}

<style>
  .scope {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    padding: 0.35rem 0.25rem 0.6rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .scope.member {
    padding-left: 0.75rem;
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
  .stop {
    font-size: 0.72rem;
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
