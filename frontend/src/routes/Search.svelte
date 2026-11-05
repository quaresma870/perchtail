<script lang="ts">
  import { onMount } from 'svelte'
  import { push, replace, router } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import SearchScopePicker from '../lib/components/SearchScopePicker.svelte'
  import {
    loadStoredScope,
    parseScopeParam,
    scopeKey,
    scopeParam,
    searchQueryString,
    storeScope,
    type ScopeItem,
    type SearchScope,
  } from '../lib/search-scope'
  import { filterSourcesByNameOrHost } from '../lib/source-match'
  import type { SearchHit, Source } from '../lib/types'

  let query = ''
  let scope: SearchScope = { items: [], path: '' }
  let hits: SearchHit[] = []
  let allSources: Source[] = []
  let sourcesById: Record<number, Source> = {}
  let matchingSources: Source[] = []
  let loading = false
  let searched = false
  let error = ''

  async function loadSources() {
    try {
      allSources = await api.get<Source[]>('/sources')
      sourcesById = Object.fromEntries(allSources.map((s) => [s.id, s]))
    } catch {
      // Best-effort — a missing name just falls back to "source #<id>" below,
      // the search itself doesn't depend on this.
    }
  }

  async function runSearch() {
    const trimmed = query.trim()
    searched = true
    // Matching sources by name/host is a plain client-side filter over the
    // already-fetched, RBAC-scoped source list (see lib/source-match.ts) —
    // metadata, not indexed content, so it's always current and doesn't
    // need a round-trip of its own.
    matchingSources = filterSourcesByNameOrHost(scopedSources(), trimmed)
    syncUrl()
    if (!trimmed) {
      hits = []
      return
    }
    loading = true
    error = ''
    try {
      hits = await api.get<SearchHit[]>(`/search?${searchQueryString(trimmed, scope)}`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Search failed'
    } finally {
      loading = false
    }
  }

  function sourceName(id: number): string {
    return sourcesById[id]?.name ?? `source #${id}`
  }

  function openHit(hit: SearchHit) {
    const params = new URLSearchParams({ path: hit.file_path, line: String(hit.line_number) })
    push(`/viewer/${hit.source_id}?${params.toString()}`)
  }

  function openSource(source: Source) {
    push(`/viewer/${source.id}`)
  }

  // Only what can be named from the user's own source list survives from
  // a URL or from storage -- a scope item the user can no longer see is
  // dropped rather than shown as a bare id.
  function knownItems(): Map<string, ScopeItem> {
    const known = new Map<string, ScopeItem>()
    for (const source of allSources) {
      if (source.customer_id !== null) {
        const item = { kind: 'customer' as const, id: source.customer_id, name: source.customer_name ?? '' }
        known.set(scopeKey(item), item)
      }
      for (const folder of source.folder_path) {
        const item = { kind: 'folder' as const, id: folder.id, name: folder.name }
        known.set(scopeKey(item), item)
      }
      known.set(`source:${source.id}`, { kind: 'source', id: source.id, name: source.name })
    }
    return known
  }

  // Sources the name/host matches are limited to: the chosen sources, and
  // everything under a chosen customer or folder.
  function scopedSources(): Source[] {
    if (scope.items.length === 0) return allSources
    const keys = new Set(scope.items.map(scopeKey))
    return allSources.filter(
      (s) =>
        keys.has(`source:${s.id}`) ||
        (s.customer_id !== null && keys.has(`customer:${s.customer_id}`)) ||
        s.folder_path.some((f) => keys.has(`folder:${f.id}`)),
    )
  }

  function syncUrl() {
    const params = new URLSearchParams()
    if (query.trim()) params.set('q', query.trim())
    if (scope.items.length) params.set('scope', scopeParam(scope.items))
    if (scope.path.trim()) params.set('path', scope.path.trim())
    const qs = params.toString()
    replace(qs ? `/search?${qs}` : '/search')
  }

  function changeScope(next: SearchScope) {
    scope = next
    storeScope(scope)
    if (searched && query.trim()) runSearch()
    else syncUrl()
  }

  onMount(async () => {
    // Read the URL before anything is awaited: setting the query only after
    // the source list loads would wipe whatever the user typed meanwhile.
    const params = new URLSearchParams(router.querystring ?? '')
    const fromUrl = params.has('scope') || params.has('path')
    const stored = fromUrl ? null : loadStoredScope()
    const pendingItems = stored ? stored.items : parseScopeParam(params.get('scope'))
    const pendingPath = stored ? stored.path : (params.get('path') ?? '')
    if (params.has('q')) query = params.get('q') ?? ''
    scope = { items: [], path: pendingPath }
    const untouched = scope

    await loadSources()
    // Scope items need names from the source list; apply them unless the
    // user already changed the scope.
    if (scope === untouched) {
      const known = knownItems()
      scope = {
        items: pendingItems
          .map((i) => known.get(scopeKey(i)))
          .filter((i): i is ScopeItem => i !== undefined),
        path: pendingPath,
      }
    }
    // A query from the URL, or a search the user ran before the list
    // arrived, runs (again) with the scope and source names in place.
    if (query.trim() && (params.has('q') || searched)) runSearch()
  })
</script>

<div class="page">
  <h1>Search</h1>

  <form class="search-form" on:submit|preventDefault={runSearch}>
    <input
      class="input"
      type="search"
      placeholder="Search indexed log content…"
      bind:value={query}
    />
    <button class="btn btn-primary" type="submit" disabled={loading}>
      {loading ? 'Searching…' : 'Search'}
    </button>
  </form>

  <SearchScopePicker
    sources={allSources}
    {scope}
    on:change={(e) => changeScope(e.detail)}
  />

  {#if error}
    <p class="error">{error}</p>
  {:else if loading}
    <p class="hint">Searching…</p>
  {:else if searched && hits.length === 0 && matchingSources.length === 0}
    <p class="hint">
      No matches. Only sources with full-text search enabled (an opt-in per source, in its editor)
      are indexed, and indexing runs periodically in the background — a very recently written line
      may not be searchable yet.
    </p>
  {:else if searched}
    {#if matchingSources.length > 0}
      <div class="section">
        <h2>Sources matching "{query.trim()}"</h2>
        <ul class="results">
          {#each matchingSources as source (source.id)}
            <li>
              <button class="result" on:click={() => openSource(source)}>
                <div class="result-meta">
                  <span class="badge badge-accent">{source.name}</span>
                  <span class="path">{source.host}</span>
                </div>
              </button>
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if hits.length > 0}
      <div class="section">
        {#if matchingSources.length > 0}
          <h2>Matching content</h2>
        {/if}
        <ul class="results">
          {#each hits as hit, i (i)}
            <li>
              <button class="result" on:click={() => openHit(hit)}>
                <div class="result-meta">
                  <span class="badge badge-accent">{sourceName(hit.source_id)}</span>
                  <span class="path">{hit.file_path}</span>
                  <span class="line">:{hit.line_number}</span>
                  {#if hit.matched_field === 'path'}
                    <span class="badge badge-muted">filename match</span>
                  {/if}
                </div>
                <div class="snippet">{@html hit.snippet_html}</div>
              </button>
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 760px;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .search-form {
    display: flex;
    gap: 0.6rem;
  }
  .search-form input {
    flex: 1;
  }
  .section {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .section h2 {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-muted);
    margin: 0;
  }
  .results {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .result {
    display: block;
    width: 100%;
    text-align: left;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    padding: 0.7rem 0.9rem;
    cursor: pointer;
  }
  .result:hover {
    border-color: var(--accent-border);
    background: var(--bg-hover);
  }
  .result-meta {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
    font-size: 0.78rem;
  }
  .result-meta .path {
    color: var(--text-muted);
    font-family: var(--font-mono);
  }
  .result-meta .line {
    color: var(--text-faint);
    font-family: var(--font-mono);
  }
  .snippet {
    font-family: var(--font-mono);
    font-size: 0.83rem;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .snippet :global(mark) {
    background: var(--warning-soft);
    color: var(--warning);
    border-radius: 3px;
    padding: 0 0.15rem;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .badge-muted {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
