<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import { api, ApiError } from '../api'
  import type { BrowseEntry } from '../types'

  export let sourceId: number
  export let entry: BrowseEntry
  export let depth = 0
  // Path of the nearest ancestor archive we're inside, or null for a normal
  // filesystem node. The backend only supports one level of archive
  // expansion (browse can't descend into a sub-path of a virtual archive
  // folder — see api/archive.py), so a directory-flagged member inside an
  // archive is a dead end, not further expandable.
  export let archiveRoot: string | null = null

  const dispatch = createEventDispatcher<{
    open: { path: string; member: string | null; name: string }
  }>()

  let expanded = false
  let children: BrowseEntry[] = []
  let loaded = false
  let loading = false
  let error = ''

  const canExpand = (entry.is_dir || entry.is_archive) && archiveRoot === null

  function downloadHref(): string | null {
    if (entry.is_dir && !entry.is_archive && archiveRoot === null) {
      return `/sources/${sourceId}/download-zip?path=${encodeURIComponent(entry.path)}`
    }
    if (!entry.is_dir) {
      const params = new URLSearchParams({ path: archiveRoot ?? entry.path })
      if (archiveRoot !== null) {
        params.set('member', entry.path.slice(archiveRoot.length + 1))
      }
      return `/sources/${sourceId}/download?${params.toString()}`
    }
    return null
  }

  async function toggle() {
    if (canExpand) {
      if (!loaded) {
        loading = true
        error = ''
        try {
          children = await api.get<BrowseEntry[]>(
            `/sources/${sourceId}/browse?path=${encodeURIComponent(entry.path)}`,
          )
          loaded = true
        } catch (err) {
          error = err instanceof ApiError ? err.detail : 'Failed to load'
        } finally {
          loading = false
        }
      }
      expanded = !expanded
    } else if (!entry.is_dir) {
      if (archiveRoot !== null) {
        const member = entry.path.slice(archiveRoot.length + 1)
        dispatch('open', { path: archiveRoot, member, name: entry.name })
      } else {
        dispatch('open', { path: entry.path, member: null, name: entry.name })
      }
    }
  }
</script>

<div class="node">
  <div class="row" class:inert={entry.is_dir && archiveRoot !== null}>
    <button class="label" style="padding-left: {depth * 14}px" on:click={toggle}>
      {#if canExpand}
        <span class="chevron">{expanded ? '▾' : '▸'}</span>
      {:else}
        <span class="chevron blank"></span>
      {/if}
      <span class="name">{entry.name}{entry.is_archive ? ' 📦' : ''}</span>
    </button>
    {#if downloadHref()}
      <a class="download" href={downloadHref()} target="_blank" rel="noreferrer" title="Download">
        ⬇
      </a>
    {/if}
  </div>

  {#if expanded}
    {#if loading}
      <div class="hint" style="padding-left: {(depth + 1) * 14}px">Loading…</div>
    {:else if error}
      <div class="error" style="padding-left: {(depth + 1) * 14}px">{error}</div>
    {:else}
      {#each children as child (child.path)}
        <svelte:self
          {sourceId}
          entry={child}
          depth={depth + 1}
          archiveRoot={entry.is_archive ? entry.path : archiveRoot}
          on:open
        />
      {/each}
      {#if children.length === 0}
        <div class="hint" style="padding-left: {(depth + 1) * 14}px">empty</div>
      {/if}
    {/if}
  {/if}
</div>

<style>
  .row {
    display: flex;
    align-items: center;
    width: 100%;
  }
  .row:hover {
    background: #edf1fb;
  }
  .row.inert {
    color: #999;
  }
  .row.inert:hover {
    background: none;
  }
  .label {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    flex: 1;
    min-width: 0;
    text-align: left;
    border: none;
    background: none;
    padding: 0.18rem 0.4rem;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .row.inert .label {
    cursor: default;
  }
  .download {
    flex: 0 0 auto;
    padding: 0 0.5rem;
    color: #888;
    text-decoration: none;
    font-size: 0.8rem;
  }
  .download:hover {
    color: #2f6fed;
  }
  .chevron {
    width: 0.9rem;
    display: inline-block;
    color: #888;
  }
  .chevron.blank {
    visibility: hidden;
  }
  .hint,
  .error {
    font-size: 0.78rem;
    color: #888;
    padding: 0.15rem 0.4rem;
  }
  .error {
    color: #c0392b;
  }
</style>
