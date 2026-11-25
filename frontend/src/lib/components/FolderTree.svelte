<script lang="ts">
  import { createEventDispatcher, getContext, onDestroy, onMount } from 'svelte'
  import { get } from 'svelte/store'
  import { explorerExpanded, setExpanded } from '../viewer-tabs'
  import { ApiError } from '../api'
  import { tabKey, memberPath } from '../tab-key'
  import { downloadHref } from '../download-href'
  import {
    EXPLORER_MENUS,
    fileLink,
    fileMenu,
    folderExpandKeys,
    folderMenu,
    tabPathText,
    type ExplorerMenu,
    type ExplorerMenus,
  } from '../context-menu'
  import { registerFolder } from '../explorer-refresh'
  import { advancedFilter, coveringScope, scopeContext, scopeKey, scopes, toggleScopeIn } from '../explorer-scope'
  import { sortEntries } from '../explorer-sort'
  import { fileDetails, formatSize } from '../file-details'
  import { explorerSort } from '../preferences'
  import { archiveChildren, archiveMembersUrl, fetchListing, truncatedNotice } from '../listing'
  import type { BrowseEntry } from '../types'

  export let sourceId: number
  export let entry: BrowseEntry
  export let depth = 0
  // Path of the nearest ancestor archive we're inside, or null for a normal
  // filesystem node. Folders inside an archive expand like real ones (the
  // backend lists archive members as a tree), but an archive nested inside
  // another archive is not opened.
  export let archiveRoot: string | null = null
  // The tab key of the file currently open in the viewer, so this node can
  // highlight itself the same way an editor's file tree does.
  export let activeKey: string | null = null
  // Whether the user holds `download` on this source -- without it the
  // per-entry download icon isn't shown (the endpoint would refuse anyway,
  // #174).
  export let canDownload = true
  // Just appeared in an auto-refresh (#272): highlighted for a moment.
  export let isNew = false
  // Inside an archive: every member of it, listed once when the archive was
  // expanded and kept while it's open (#347), so a folder inside it opens
  // without fetching the archive again.
  export let archiveMembers: BrowseEntry[] | null = null
  // Inside an archive: lists the archive again (Refresh in its menu).
  export let relistArchive: (() => void) | null = null

  // `mode`, from the right-click menu (#287): Open (even with Compare
  // armed), Open with Tail, Open the end, or Compare with the active tab.
  const dispatch = createEventDispatcher<{
    open: { path: string; member: string | null; name: string; size: number; mode?: OpenMode }
  }>()
  type OpenMode = 'open' | 'tail' | 'end' | 'compare'

  const menus = getContext<ExplorerMenus | undefined>(EXPLORER_MENUS)
  let labelButton: HTMLElement

  let expanded = false
  let children: BrowseEntry[] = []
  let loaded = false
  let loading = false
  let error = ''
  // The listing was capped on the server (#347): what to say about it.
  let truncatedText = ''
  // This archive's members, when this node is an archive.
  let ownMembers: BrowseEntry[] | null = null

  const canExpand = entry.is_dir || (entry.is_archive && archiveRoot === null)
  const expandKey = `dir:${sourceId}:${archiveRoot ?? ''}:${entry.path}`

  $: ownKey =
    archiveRoot !== null
      ? tabKey(sourceId, archiveRoot, memberPath(entry.path, archiveRoot))
      : tabKey(sourceId, entry.path, null)
  $: isActive = !entry.is_dir && activeKey === ownKey

  $: ownDownloadHref = canDownload ? downloadHref(sourceId, entry, archiveRoot) : null

  const browseUrl = () => `/sources/${sourceId}/browse?path=${encodeURIComponent(entry.path)}`

  // Folders re-list themselves while open (#272); archives don't, since
  // listing one reads the archive (a .tar.gz whole).
  const canRefresh = entry.is_dir && archiveRoot === null
  let newPaths = new Set<string>()
  let newTimer: ReturnType<typeof setTimeout> | null = null
  let refreshing = false

  /** Re-lists this folder in place: open subfolders and the scroll stay,
   * new files are highlighted for a few seconds. */
  async function refresh(): Promise<boolean> {
    if (refreshing) return true
    refreshing = true
    try {
      const listing = await fetchListing(`${browseUrl()}&refresh=true`)
      const fresh = listing.entries
      truncatedText = listing.truncated ? truncatedNotice(listing) : ''
      const known = new Set(children.map((c) => c.path))
      const added = fresh.filter((c) => !known.has(c.path)).map((c) => c.path)
      children = fresh
      error = ''
      if (added.length > 0) {
        newPaths = new Set(added)
        if (newTimer) clearTimeout(newTimer)
        newTimer = setTimeout(() => (newPaths = new Set()), 5000)
      }
      refreshError = ''
      return true
    } catch (err) {
      // Gone (or hidden by a rule change): its parent's next listing drops
      // it -- not a reason to hold back the rest of the source.
      return err instanceof ApiError && err.status === 404
    } finally {
      refreshing = false
    }
  }

  let refreshError = ''
  async function refreshNow() {
    refreshError = (await refresh()) ? '' : "Couldn't refresh this folder"
  }

  // Advanced filter (#274): a folder (not one inside an archive) as a scope;
  // one already covered by a selected parent shows ticked and fixed.
  $: ownScope = { sourceId, path: entry.path }
  $: inScope = $scopes.some((s) => scopeKey(s) === scopeKey(ownScope))
  $: coveredBy = coveringScope($scopes, ownScope, $scopeContext)

  let unregister: (() => void) | null = null
  $: if (canRefresh && expanded && loaded && !unregister) {
    unregister = registerFolder({ sourceId, path: entry.path, refresh })
  } else if (unregister && !(expanded && loaded)) {
    unregister()
    unregister = null
  }
  onDestroy(() => {
    unregister?.()
    if (newTimer) clearTimeout(newTimer)
  })

  async function load() {
    if (entry.is_archive && archiveRoot === null) {
      const listing = await fetchListing(archiveMembersUrl(sourceId, entry.path))
      ownMembers = listing.entries
      children = archiveChildren(listing.entries, entry.path)
      truncatedText = listing.truncated ? truncatedNotice(listing) : ''
    } else if (archiveRoot !== null && archiveMembers !== null) {
      children = archiveChildren(archiveMembers, entry.path)
    } else {
      const listing = await fetchListing(browseUrl())
      children = listing.entries
      truncatedText = listing.truncated ? truncatedNotice(listing) : ''
    }
  }

  // A folder inside an archive takes its children from the archive's
  // members, again when the archive is listed again.
  $: if (loaded && entry.is_dir && archiveRoot !== null && archiveMembers !== null) {
    children = archiveChildren(archiveMembers, entry.path)
  }

  /** Refresh of an archive: its members listed again. */
  async function relistOwnArchive() {
    try {
      await load()
      error = ''
      refreshError = ''
    } catch (err) {
      refreshError = err instanceof ApiError ? err.detail : "Couldn't refresh this archive"
    }
  }

  // Right-click menu (#287).
  $: isArchiveNode = entry.is_archive && archiveRoot === null
  $: asFile = !entry.is_dir && !isArchiveNode

  /** The path Copy path gives: a member after its archive. */
  function pathText(): string {
    return archiveRoot !== null ? tabPathText(archiveRoot, memberPath(entry.path, archiveRoot)) : entry.path
  }

  function openAs(mode: OpenMode) {
    if (archiveRoot !== null) {
      const member = memberPath(entry.path, archiveRoot)
      dispatch('open', { path: archiveRoot, member, name: entry.name, size: entry.size, mode })
    } else {
      dispatch('open', { path: entry.path, member: null, name: entry.name, size: entry.size, mode })
    }
  }

  /** Collapse all: this node and everything below it close, and stay
   * closed when it opens again. */
  function collapseAll() {
    explorerExpanded.update((keys) => {
      const drop = new Set(folderExpandKeys(keys, sourceId, archiveRoot, entry.path))
      return drop.size === 0 ? keys : new Set([...keys].filter((key) => !drop.has(key)))
    })
    expanded = false
  }

  function buildMenu(): ExplorerMenu {
    if (asFile) {
      return {
        label: 'File actions',
        entries: fileMenu({
          archiveMember: archiveRoot !== null,
          gzip: entry.name.toLowerCase().endsWith('.gz'),
          otherTabActive: activeKey !== null && activeKey !== ownKey,
          canDownload: ownDownloadHref !== null,
        }),
        run: runFileMenu,
      }
    }
    return {
      label: 'Folder actions',
      entries: folderMenu({
        inArchive: archiveRoot !== null,
        archive: isArchiveNode,
        listed: expanded && loaded,
        expanded,
        canDownload: ownDownloadHref !== null,
      }),
      run: runFolderMenu,
    }
  }

  function runFileMenu(id: string) {
    switch (id) {
      case 'open':
        return openAs('open')
      case 'open-tail':
        return openAs('tail')
      case 'open-end':
        return openAs('end')
      case 'compare':
        return openAs('compare')
      case 'download':
        if (ownDownloadHref) window.open(ownDownloadHref, '_blank', 'noreferrer')
        return
      case 'copy-path':
        return menus?.copy(pathText())
      case 'copy-link':
        return menus?.copy(fileLink(window.location.href, sourceId, entry.path))
    }
  }

  function runFolderMenu(id: string) {
    switch (id) {
      case 'refresh':
        if (isArchiveNode) return void relistOwnArchive()
        if (archiveRoot !== null) return relistArchive?.()
        return void refreshNow()
      case 'search-here':
        return menus?.searchHere({ sourceId, path: entry.path })
      case 'collapse-all':
        return collapseAll()
      case 'download':
        if (ownDownloadHref) window.open(ownDownloadHref, '_blank', 'noreferrer')
        return
      case 'copy-path':
        return menus?.copy(pathText())
    }
  }

  async function toggle() {
    if (canExpand) {
      if (!loaded) {
        loading = true
        error = ''
        try {
          await load()
          loaded = true
        } catch (err) {
          error = err instanceof ApiError ? err.detail : 'Failed to load'
        } finally {
          loading = false
        }
      } else if (!expanded && canRefresh) {
        // Re-opened: show what was there and list it again behind it.
        refresh()
      }
      expanded = !expanded
      setExpanded(expandKey, expanded)
    } else if (!entry.is_dir) {
      if (archiveRoot !== null) {
        const member = memberPath(entry.path, archiveRoot)
        dispatch('open', { path: archiveRoot, member, name: entry.name, size: entry.size })
      } else {
        dispatch('open', { path: entry.path, member: null, name: entry.name, size: entry.size })
      }
    }
  }

  onMount(() => {
    if (canExpand && get(explorerExpanded).has(expandKey)) toggle()
  })
</script>

<div class="node">
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="row"
    class:inert={entry.is_dir && archiveRoot !== null}
    class:active={isActive}
    class:new={isNew}
    on:contextmenu={(e) => menus?.mouse(e, labelButton, buildMenu)}
  >
    <!-- aria-label: the file name only, not the size shown next to it; the
         size and modification time are in the hover text. -->
    {#if $advancedFilter && canRefresh}
      <input
        type="checkbox"
        class="scope-check"
        checked={inScope || Boolean(coveredBy)}
        disabled={Boolean(coveredBy)}
        aria-label="Filter files in {entry.name}"
        title={coveredBy ? 'Already included through a folder above' : 'Filter files in this folder'}
        on:change={() => toggleScopeIn(ownScope)}
      />
    {/if}
    <button
      class="label"
      style="padding-left: {depth * 14}px"
      aria-label={entry.is_dir ? entry.name : `${entry.name}, ${formatSize(entry.size)}`}
      title={entry.is_dir ? undefined : fileDetails(entry)}
      bind:this={labelButton}
      on:click={toggle}
      on:keydown={(e) => menus?.key(e, buildMenu)}
    >
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
      {#if !entry.is_dir}
        <span class="size">{formatSize(entry.size)}</span>
      {/if}
    </button>
    {#if canRefresh && expanded}
      <button class="refresh" title="Refresh this folder" aria-label="Refresh {entry.name}" on:click={refreshNow}>⟳</button>
    {/if}
    {#if ownDownloadHref}
      <a class="download" href={ownDownloadHref} target="_blank" rel="noreferrer" title="Download">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 3v12" />
          <path d="m6 11 6 6 6-6" />
          <path d="M5 21h14" />
        </svg>
      </a>
    {/if}
  </div>

  {#if refreshError}
    <div class="error" style="padding-left: {(depth + 1) * 14}px">{refreshError}</div>
  {/if}
  {#if expanded}
    {#if loading}
      <div class="hint" style="padding-left: {(depth + 1) * 14}px">Loading…</div>
    {:else if error}
      <div class="error" style="padding-left: {(depth + 1) * 14}px">{error}</div>
    {:else}
      {#each sortEntries(children, $explorerSort) as child (child.path)}
        <svelte:self
          {sourceId}
          entry={child}
          isNew={newPaths.has(child.path)}
          depth={depth + 1}
          archiveRoot={entry.is_archive ? entry.path : archiveRoot}
          archiveMembers={entry.is_archive ? ownMembers : archiveMembers}
          relistArchive={isArchiveNode ? relistOwnArchive : relistArchive}
          {activeKey}
          {canDownload}
          on:open
        />
      {/each}
      {#if children.length === 0}
        <div class="hint" style="padding-left: {(depth + 1) * 14}px">empty</div>
      {/if}
      {#if truncatedText}
        <div class="hint truncated" role="status" style="padding-left: {(depth + 1) * 14}px">{truncatedText}</div>
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
  .scope-check {
    flex: 0 0 auto;
    accent-color: var(--accent);
  }
  .row.new {
    background: var(--success-soft);
    transition: background 1s;
  }
  .size {
    margin-left: auto;
    padding-left: 0.5rem;
    font-size: 0.72rem;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    flex: 0 0 auto;
  }
  .refresh {
    flex: 0 0 auto;
    border: none;
    background: none;
    color: var(--text-faint);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0 0.4rem;
    visibility: hidden;
  }
  .row:hover .refresh {
    visibility: visible;
  }
  .refresh:hover {
    color: var(--accent-hover);
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
