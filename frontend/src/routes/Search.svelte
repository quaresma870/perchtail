<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import type { SearchHit, Source } from '../lib/types'

  let query = ''
  let hits: SearchHit[] = []
  let sourcesById: Record<number, Source> = {}
  let loading = false
  let searched = false
  let error = ''

  async function loadSources() {
    try {
      const sources = await api.get<Source[]>('/sources')
      sourcesById = Object.fromEntries(sources.map((s) => [s.id, s]))
    } catch {
      // Best-effort — a missing name just falls back to "source #<id>" below,
      // the search itself doesn't depend on this.
    }
  }

  async function runSearch() {
    const trimmed = query.trim()
    searched = true
    if (!trimmed) {
      hits = []
      return
    }
    loading = true
    error = ''
    try {
      hits = await api.get<SearchHit[]>(`/search?q=${encodeURIComponent(trimmed)}`)
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

  onMount(loadSources)
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

  {#if error}
    <p class="error">{error}</p>
  {:else if loading}
    <p class="hint">Searching…</p>
  {:else if searched && hits.length === 0}
    <p class="hint">
      No matches. Only sources with full-text search enabled (an opt-in per source, in its editor)
      are indexed, and indexing runs periodically in the background — a very recently written line
      may not be searchable yet.
    </p>
  {:else if hits.length > 0}
    <ul class="results">
      {#each hits as hit, i (i)}
        <li>
          <button class="result" on:click={() => openHit(hit)}>
            <div class="result-meta">
              <span class="badge badge-accent">{sourceName(hit.source_id)}</span>
              <span class="path">{hit.file_path}</span>
              <span class="line">:{hit.line_number}</span>
            </div>
            <div class="snippet">{@html hit.snippet_html}</div>
          </button>
        </li>
      {/each}
    </ul>
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
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
