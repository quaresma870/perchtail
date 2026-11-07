<script lang="ts">
  // One source or folder of the advanced filter (#274, #284): the files
  // whose names match the filter, in the explorer's order. Every folder
  // below is searched right away, breadth first, through the shared
  // listing limiter (a few listings at a time, so sources aren't flooded),
  // with a Stop button. No cap on folders or time; only a depth guard
  // against links that loop, and it says so when it's hit. Every listing
  // is the live browse, under the rules.
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

  // A link to a folder above it (allowed inside the base over SSH and the
  // agent) would make the search go down forever; no real log tree is
  // this deep. Folders past it aren't searched, and the result says where.
  const LOOP_GUARD_DEPTH = 32

  const dispatch = createEventDispatcher<{
    open: { sourceId: number; sourceName: string; path: string; member: null; name: string }
  }>()

  // Folder path -> its entries, for every folder listed so far.
  let listings = new Map<string, BrowseEntry[]>()
  let state: 'searching' | 'done' | 'stopped' = 'searching'
  let error = ''
  // Folders below that couldn't be listed, and the first folder where the
  // loop guard stopped going down (shown to the user).
  let unreadable = 0
  let tooDeep: string | null = null
  let stopped = false
  let destroyed = false

  const browse = (path: string, refresh = false) =>
    api.get<BrowseEntry[]>(
      `/sources/${scope.sourceId}/browse?path=${encodeURIComponent(path)}${refresh ? '&refresh=true' : ''}`,
    )

  async function search() {
    const queue: { path: string; depth: number }[] = [{ path: scope.path, depth: 0 }]
    const inFlight = new Set<Promise<void>>()

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
          if (item.depth < LOOP_GUARD_DEPTH) queue.push({ path: entry.path, depth: item.depth + 1 })
          else tooDeep ??= entry.path
        }
      } catch (err) {
        // The ticked folder itself failing is an error; a folder below it
        // that can't be listed is skipped (and counted), the rest go on.
        if (item.path === scope.path) error = err instanceof ApiError ? err.detail : 'Failed to load'
        else unreadable += 1
      }
    }

    while ((queue.length > 0 || inFlight.size > 0) && !stopped && !destroyed) {
      while (queue.length > 0 && inFlight.size < PER_SOURCE && !stopped) {
        const task: Promise<void> = listOne(queue.shift()!).finally(() => inFlight.delete(task))
        inFlight.add(task)
      }
      if (inFlight.size > 0) await Promise.race(inFlight)
    }
    state = stopped ? 'stopped' : 'done'
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

  // Auto-refresh (#272): the ticked folder and the folders that had files,
  // through the same limiter. A refresh still running when the next one
  // is due makes that one wait for the following tick.
  let refreshing = false
  async function refresh(): Promise<boolean> {
    if (state === 'searching' || refreshing) return true
    refreshing = true
    const paths = [
      scope.path,
      ...[...listings.entries()]
        .filter(([path, entries]) => path !== scope.path && entries.some((e) => !e.is_dir))
        .map(([path]) => path),
    ]
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
    } finally {
      refreshing = false
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
  // A source of a ticked group with nothing to show is hidden once searched,
  // unless the search was incomplete: that's always shown.
  $: complete = state === 'done' && !tooDeep && unreadable === 0
  $: hidden = partOfGroup && complete && !error && matches.length === 0
  $: folderCount = `${listings.size} ${listings.size === 1 ? 'folder' : 'folders'}`
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
        <div class="hint">Searching… {folderCount} so far</div>
      {:else if matches.length === 0 && matcher}
        <div class="hint">{files.length === 0 ? 'No files here.' : 'No file names match.'}</div>
      {/if}
      {#if state === 'stopped'}
        <div class="warn">Stopped after {folderCount}: the folders not reached yet weren't searched.</div>
      {:else if state === 'done'}
        <div class="hint">Searched {folderCount}.</div>
      {/if}
      {#if tooDeep}
        <div class="warn" title={tooDeep}>
          Not searched below {LOOP_GUARD_DEPTH} levels ({relative(tooDeep)}): probably a link that loops back
          to a folder above. Tick a folder further down to search there.
        </div>
      {/if}
      {#if unreadable > 0}
        <div class="warn">
          {unreadable} {unreadable === 1 ? 'folder' : 'folders'} couldn't be listed and {unreadable === 1
            ? 'was'
            : 'were'} skipped.
        </div>
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
  .warn {
    font-size: 0.78rem;
    color: var(--warning);
    padding: 0.15rem 1rem;
  }
</style>
