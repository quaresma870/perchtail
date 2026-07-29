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
  // The tab key of the file currently open in the viewer, so this node can
  // highlight itself the same way an editor's file tree does.
  export let activeKey: string | null = null

  const dispatch = createEventDispatcher<{
    open: { path: string; member: string | null; name: string }
  }>()

  let expanded = false
  let children: BrowseEntry[] = []
  let loaded = false
  let loading = false
  let error = ''

  const canExpand = (entry.is_dir || entry.is_archive) && archiveRoot === null

  $: ownKey = archiveRoot !== null ? `${archiveRoot}::${entry.path.slice(archiveRoot.length + 1)}` : `${entry.path}::`
  $: isActive = !entry.is_dir && activeKey === ownKey

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
  <div class="row" class:inert={entry.is_dir && archiveRoot !== null} class:active={isActive}>
    <button class="label" style="padding-left: {depth * 14}px" on:click={toggle}>
      {#if canExpand}
        <span class="chevron">{expanded ? '▾' : '▸'}</span>
      {:else}
        <span class="chevron blank"></span>
      {/if}
      <span class="icon" class:folder={entry.is_dir} class:archive={entry.is_archive}>
        {#if entry.is_dir}
          {#if expanded}
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path
                d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5c0-1.1.9-2 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"
              />
            </svg>
          {:else}
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
            </svg>
          {/if}
        {:else if entry.is_archive}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="3" y="4" width="18" height="4" rx="1" />
            <path d="M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8" />
            <path d="M10 12h4" />
          </svg>
        {:else}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5Z" />
            <path d="M14 2v4a2 2 0 0 0 2 2h4" />
          </svg>
        {/if}
      </span>
      <span class="name">{entry.name}</span>
    </button>
    {#if downloadHref()}
      <a class="download" href={downloadHref()} target="_blank" rel="noreferrer" title="Download">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 3v12" />
          <path d="m6 11 6 6 6-6" />
          <path d="M5 21h14" />
        </svg>
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
          {activeKey}
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
    border-radius: var(--radius-sm);
  }
  .row:hover {
    background: var(--bg-hover);
  }
  .row.active {
    background: var(--accent-soft);
  }
  .row.active .label {
    color: var(--text);
  }
  .row.inert {
    color: var(--text-faint);
  }
  .row.inert:hover {
    background: none;
  }
  .label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex: 1;
    min-width: 0;
    text-align: left;
    border: none;
    background: none;
    padding: 0.28rem 0.4rem;
    cursor: pointer;
    font-size: 0.83rem;
    color: var(--text-muted);
  }
  .row.inert .label {
    cursor: default;
  }
  .icon {
    flex: 0 0 auto;
    width: 15px;
    height: 15px;
    color: var(--text-faint);
  }
  .icon.folder {
    color: #7c93c9;
  }
  .icon.archive {
    color: var(--protocol-smb-text);
  }
  .icon svg {
    width: 100%;
    height: 100%;
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .download {
    flex: 0 0 auto;
    padding: 0 0.5rem;
    color: var(--text-faint);
    display: flex;
    align-items: center;
  }
  .download svg {
    width: 13px;
    height: 13px;
  }
  .download:hover {
    color: var(--accent-hover);
  }
  .chevron {
    width: 0.8rem;
    display: inline-block;
    color: var(--text-faint);
    font-size: 0.7rem;
  }
  .chevron.blank {
    visibility: hidden;
  }
  .hint,
  .error {
    font-size: 0.78rem;
    color: var(--text-faint);
    padding: 0.15rem 0.4rem;
  }
  .error {
    color: var(--danger);
  }
</style>
