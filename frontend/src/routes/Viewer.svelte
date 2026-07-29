<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import FolderTree from '../lib/components/FolderTree.svelte'
  import CodeMirrorPane from '../lib/components/CodeMirrorPane.svelte'
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
  $: activeTab = tabs.find((t) => t.key === activeKey) ?? null

  function tabKey(path: string, member: string | null) {
    return `${path}::${member ?? ''}`
  }

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

  async function handleOpen(event: CustomEvent<{ path: string; member: string | null; name: string }>) {
    if (sourceId === null) return
    const { path, member, name } = event.detail
    const key = tabKey(path, member)
    const existing = tabs.find((t) => t.key === key)
    if (existing) {
      activeKey = key
      return
    }

    const params = new URLSearchParams({ path })
    if (member) params.set('member', member)
    try {
      const response = await fetch(`/sources/${sourceId}/open?${params.toString()}`, {
        credentials: 'include',
      })
      if (!response.ok) {
        error = `Failed to open ${name}`
        return
      }
      const content = await response.text()
      const scratchKey = response.headers.get('x-scratch-key')
      const tab: Tab = { key, path, member, name, content, scratchKey }
      tabs = [...tabs, tab]
      activeKey = key
    } catch {
      error = `Failed to open ${name}`
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

  onMount(() => {
    if (sourceId === null) {
      loadSourcePicker()
    } else {
      loadTree()
    }
  })

  onDestroy(() => {
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
      <p>Loading…</p>
    {:else if error}
      <p class="error">{error}</p>
    {:else}
      <ul>
        {#each sources as s (s.id)}
          <li>
            <button on:click={() => push(`/viewer/${s.id}`)}>
              {s.name}
              {#if s.is_system}<span class="badge">system</span>{/if}
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
            <FolderTree {sourceId} {entry} on:open={handleOpen} />
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
            <button class="tab-label" on:click={() => (activeKey = tab.key)}>{tab.name}</button>
            <button class="close" on:click={() => closeTab(tab)} aria-label={`Close ${tab.name}`}
              >×</button
            >
          </div>
        {/each}
      </div>
      {#if activeTab}
        <div class="pane-toolbar">
          <span class="hint">Ctrl/Cmd+F to search in file</span>
          <a
            class="link"
            href={`/sources/${sourceId}/download?${new URLSearchParams({ path: activeTab.path, ...(activeTab.member ? { member: activeTab.member } : {}) }).toString()}`}
            target="_blank"
            rel="noreferrer"
          >
            Download
          </a>
        </div>
        <CodeMirrorPane content={activeTab.content} />
      {:else}
        <div class="empty-state">Select a file from the tree to view it.</div>
      {/if}
    </section>
  </div>
{/if}

<style>
  .page {
    padding: 1.5rem;
  }
  .picker ul {
    list-style: none;
    padding: 0;
  }
  .picker button {
    border: none;
    background: #fff;
    padding: 0.6rem 1rem;
    width: 100%;
    text-align: left;
    border-radius: 4px;
    margin-bottom: 0.4rem;
    cursor: pointer;
  }
  .badge {
    margin-left: 0.5rem;
    font-size: 0.7rem;
    background: #2f6fed;
    color: #fff;
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
  }
  .viewer {
    flex: 1;
    display: flex;
    min-height: 0;
  }
  .tree {
    width: 280px;
    flex: 0 0 280px;
    border-right: 1px solid #ddd;
    background: #fafbfc;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .tree-header {
    padding: 0.6rem 0.75rem;
    border-bottom: 1px solid #eee;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.85rem;
  }
  .tree-body {
    flex: 1;
    overflow: auto;
    padding: 0.3rem 0;
  }
  button.link {
    border: none;
    background: none;
    color: #2f6fed;
    cursor: pointer;
    padding: 0;
    font-size: 0.8rem;
    text-align: left;
  }
  .editor-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }
  .tabs {
    display: flex;
    background: #e9ebef;
    border-bottom: 1px solid #ccc;
    overflow-x: auto;
  }
  .tab {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    border-right: 1px solid #d5d7dc;
    white-space: nowrap;
  }
  .tab.active {
    background: #fff;
  }
  .tab-label {
    border: none;
    background: none;
    padding: 0.45rem 0 0.45rem 0.8rem;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .tab .close {
    border: none;
    background: none;
    padding: 0.45rem 0.8rem 0.45rem 0;
    font-size: 0.8rem;
    cursor: pointer;
    color: #999;
  }
  .tab .close:hover {
    color: #c0392b;
  }
  .pane-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.35rem 0.75rem;
    background: #fff;
    border-bottom: 1px solid #eee;
    font-size: 0.78rem;
  }
  .pane-toolbar .hint {
    padding: 0;
    color: #999;
  }
  .pane-toolbar .link {
    color: #2f6fed;
    text-decoration: none;
  }
  .empty-state {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #999;
    font-size: 0.9rem;
  }
  .hint {
    padding: 0.5rem 0.75rem;
    font-size: 0.8rem;
    color: #888;
  }
  .error {
    color: #c0392b;
    padding: 0.5rem 0.75rem;
  }
</style>
