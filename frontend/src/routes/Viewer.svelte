<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import { push, router } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import FolderTree from '../lib/components/FolderTree.svelte'
  import CodeMirrorPane from '../lib/components/CodeMirrorPane.svelte'
  import FindInDocumentPanel from '../lib/components/FindInDocumentPanel.svelte'
  import { tabKey } from '../lib/tab-key'
  import type { BrowseEntry, Source } from '../lib/types'

  export let params: { sourceId?: string } = {}

  interface Tab {
    key: string
    path: string
    member: string | null
    name: string
    content: string
    scratchKey: string | null
  }

  const sourceId = params.sourceId ? Number(params.sourceId) : null

  let sources: Source[] = []
  let source: Source | null = null
  let rootEntries: BrowseEntry[] = []
  let loading = true
  let error = ''

  let tabs: Tab[] = []
  let activeKey: string | null = null
  let paneRef: CodeMirrorPane | null = null
  // Deliberately not reset on tab switch -- if it's open, it stays open and
  // re-searches whatever tab becomes active against the same query, rather
  // than forcing it closed and reopened for every file.
  let findAllOpen = false
  $: activeTab = tabs.find((t) => t.key === activeKey) ?? null

  async function loadSourcePicker() {
    loading = true
    try {
      sources = await api.get<Source[]>('/sources')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load sources'
    } finally {
      loading = false
    }
  }

  async function loadTree() {
    if (sourceId === null) return
    loading = true
    error = ''
    try {
      source = await api.get<Source>(`/sources/${sourceId}`)
      rootEntries = await api.get<BrowseEntry[]>(`/sources/${sourceId}/browse?path=`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load source'
    } finally {
      loading = false
    }
  }

  async function openFile(path: string, member: string | null, name: string): Promise<boolean> {
    if (sourceId === null) return false
    const key = tabKey(path, member)
    const existing = tabs.find((t) => t.key === key)
    if (existing) {
      activeKey = key
      return true
    }

    const fetchParams = new URLSearchParams({ path })
    if (member) fetchParams.set('member', member)
    try {
      const response = await fetch(`/sources/${sourceId}/open?${fetchParams.toString()}`, {
        credentials: 'include',
      })
      if (!response.ok) {
        error = `Failed to open ${name}`
        return false
      }
      const content = await response.text()
      const scratchKey = response.headers.get('x-scratch-key')
      const tab: Tab = { key, path, member, name, content, scratchKey }
      tabs = [...tabs, tab]
      activeKey = key
      return true
    } catch {
      error = `Failed to open ${name}`
      return false
    }
  }

  async function handleOpen(event: CustomEvent<{ path: string; member: string | null; name: string }>) {
    const { path, member, name } = event.detail
    await openFile(path, member, name)
  }

  // Search click-through (Search.svelte pushes here with ?path=...&line=...)
  // — opens the file same as clicking it in the tree, then scrolls the
  // CodeMirror pane to the matched line once its content has rendered.
  async function openFromDeepLink(path: string, line: number | null) {
    const name = path.split('/').pop() ?? path
    const opened = await openFile(path, null, name)
    if (opened && line !== null) {
      await tick()
      paneRef?.scrollToLine(line)
    }
  }

  async function closeTab(tab: Tab) {
    tabs = tabs.filter((t) => t.key !== tab.key)
    if (activeKey === tab.key) {
      activeKey = tabs.length > 0 ? tabs[tabs.length - 1].key : null
    }
    if (tab.scratchKey && sourceId !== null) {
      try {
        await api.post(`/sources/${sourceId}/close`, { path: tab.path, member: tab.member })
      } catch {
        // best-effort — the idle-sweep backstop will clean this up regardless
      }
    }
  }

  // Ctrl/Cmd+F is meant to search the open file via CodeMirror's own search
  // panel, not the browser's find bar — but a plain keydown on the editor
  // DOM isn't reliable (see CodeMirrorPane's openSearch doc comment), so
  // it's intercepted here at the window level instead, while a tab is open.
  function handleKeydown(event: KeyboardEvent) {
    if (!activeTab) return
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault()
      paneRef?.openSearch()
    }
  }

  onMount(() => {
    if (sourceId === null) {
      loadSourcePicker()
    } else {
      loadTree()
      const deepLink = new URLSearchParams(router.querystring ?? '')
      const deepLinkPath = deepLink.get('path')
      if (deepLinkPath) {
        const line = deepLink.get('line')
        openFromDeepLink(deepLinkPath, line ? Number(line) : null)
      }
    }
    window.addEventListener('keydown', handleKeydown)
  })

  onDestroy(() => {
    window.removeEventListener('keydown', handleKeydown)
    for (const tab of tabs) {
      if (tab.scratchKey && sourceId !== null) {
        api.post(`/sources/${sourceId}/close`, { path: tab.path, member: tab.member }).catch(() => {})
      }
    }
  })
</script>

{#if sourceId === null}
  <div class="picker page">
    <h1>Choose a source to browse</h1>
    {#if loading}
      <p class="hint">Loading…</p>
    {:else if error}
      <p class="error">{error}</p>
    {:else}
      <ul>
        {#each sources as s (s.id)}
          <li>
            <button class="card" on:click={() => push(`/viewer/${s.id}`)}>
              {s.name}
              {#if s.is_system}<span class="badge badge-accent">system</span>{/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{:else}
  <div class="viewer">
    <aside class="tree">
      <div class="tree-header">
        <button class="link" on:click={() => push('/viewer')}>← sources</button>
        <strong>{source?.name ?? ''}</strong>
      </div>
      {#if loading}
        <p class="hint">Loading…</p>
      {:else if error}
        <p class="error">{error}</p>
      {:else}
        <div class="tree-body">
          {#each rootEntries as entry (entry.path)}
            <FolderTree {sourceId} {entry} {activeKey} on:open={handleOpen} />
          {/each}
          {#if rootEntries.length === 0}
            <p class="hint">Nothing visible here — check the source's rules.</p>
          {/if}
        </div>
      {/if}
    </aside>

    <section class="editor-area">
      <div class="tabs">
        {#each tabs as tab (tab.key)}
          <div class="tab" class:active={tab.key === activeKey}>
            <button class="tab-label" on:click={() => (activeKey = tab.key)}>
              <svg class="file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5Z" />
                <path d="M14 2v4a2 2 0 0 0 2 2h4" />
              </svg>
              {tab.name}
            </button>
            <button class="close" on:click={() => closeTab(tab)} aria-label={`Close ${tab.name}`}
              >×</button
            >
          </div>
        {/each}
      </div>
      {#if activeTab}
        <div class="pane-toolbar">
          <div class="toolbar-left">
            <span class="hint">⌕ Ctrl/Cmd+F to search in file</span>
            <button
              class="btn-toggle"
              class:active={findAllOpen}
              on:click={() => (findAllOpen = !findAllOpen)}
            >
              Find All
            </button>
          </div>
          <a
            class="link"
            href={`/sources/${sourceId}/download?${new URLSearchParams({ path: activeTab.path, ...(activeTab.member ? { member: activeTab.member } : {}) }).toString()}`}
            target="_blank"
            rel="noreferrer"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 3v12" />
              <path d="m6 11 6 6 6-6" />
              <path d="M5 21h14" />
            </svg>
            Download
          </a>
        </div>
        <CodeMirrorPane bind:this={paneRef} content={activeTab.content} />
        {#if findAllOpen}
          <FindInDocumentPanel
            content={activeTab.content}
            on:jump={(e) => paneRef?.scrollToLine(e.detail.line)}
            on:close={() => (findAllOpen = false)}
          />
        {/if}
      {:else}
        <div class="empty-state">Select a file from the tree to view it.</div>
      {/if}
    </section>
  </div>
{/if}

<style>
  .page {
    padding: 1.75rem 2rem;
  }
  .picker ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-width: 480px;
  }
  .picker button {
    border: none;
    padding: 0.75rem 1rem;
    width: 100%;
    text-align: left;
    cursor: pointer;
    color: var(--text);
    font-size: 0.92rem;
  }
  .picker button:hover {
    border-color: var(--accent-border);
  }
  .viewer {
    flex: 1;
    display: flex;
    min-height: 0;
  }
  .tree {
    width: 280px;
    flex: 0 0 280px;
    border-right: 1px solid var(--border-soft);
    background: var(--bg-elevated);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .tree-header {
    padding: 0.7rem 0.9rem;
    border-bottom: 1px solid var(--border-soft);
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.85rem;
  }
  .tree-header strong {
    color: var(--text);
  }
  .tree-body {
    flex: 1;
    overflow: auto;
    padding: 0.4rem 0;
  }
  button.link,
  a.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    padding: 0;
    font-size: 0.8rem;
    text-align: left;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .editor-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--bg);
  }
  .tabs {
    display: flex;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
    overflow-x: auto;
  }
  .tab {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    border-right: 1px solid var(--border-soft);
    white-space: nowrap;
  }
  .tab.active {
    background: var(--bg);
  }
  .tab-label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: none;
    background: none;
    padding: 0.5rem 0 0.5rem 0.9rem;
    font-size: 0.8rem;
    cursor: pointer;
    color: var(--text-muted);
  }
  .tab.active .tab-label {
    color: var(--text);
  }
  .file-icon {
    width: 13px;
    height: 13px;
    flex: 0 0 auto;
    color: var(--text-faint);
  }
  .tab .close {
    border: none;
    background: none;
    padding: 0.5rem 0.9rem 0.5rem 0;
    font-size: 0.85rem;
    cursor: pointer;
    color: var(--text-faint);
  }
  .tab .close:hover {
    color: var(--danger);
  }
  .pane-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.4rem 0.9rem;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
    font-size: 0.78rem;
  }
  .toolbar-left {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }
  .pane-toolbar .hint {
    padding: 0;
    color: var(--text-faint);
  }
  .btn-toggle {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-muted);
    border-radius: 999px;
    padding: 0.15rem 0.65rem;
    font-size: 0.72rem;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-toggle:hover {
    border-color: var(--accent-border);
    color: var(--text);
  }
  .btn-toggle.active {
    background: var(--accent-soft);
    border-color: var(--accent-border);
    color: var(--accent-hover);
  }
  .pane-toolbar .link svg {
    width: 13px;
    height: 13px;
  }
  .empty-state {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-faint);
    font-size: 0.9rem;
  }
  .hint {
    padding: 0.5rem 0.9rem;
    font-size: 0.8rem;
    color: var(--text-faint);
  }
  .error {
    color: var(--danger);
    padding: 0.5rem 0.9rem;
  }
</style>
