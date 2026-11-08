<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte'
  import { api, ApiError } from '../api'
  import { formatSize } from '../file-details'
  import { ago } from '../source-status'
  import type { Source } from '../types'

  /** The explorer's one search (#300), with nothing ticked: sources (by
   * name, host, customer or folder) and file names in every source the
   * user can view, from the file-name index (#304). Results replace the
   * tree while there's text in the box. */
  export let query: string
  export let regex: boolean
  export let sources: Source[]

  interface FileHit {
    source_id: number
    path: string
    size: number
    mtime: number | null
  }
  interface IndexedSource {
    source_id: number
    name_index_enabled: boolean
    name_indexed_at: string | null
    name_index_files: number | null
    name_index_message: string | null
  }
  interface FileSearchResult {
    hits: FileHit[]
    next_after: number | null
    index_enabled: boolean
    sources: IndexedSource[]
  }

  const PAGE = 200
  const dispatch = createEventDispatcher<{
    open: { sourceId: number; sourceName: string; path: string; member: null; name: string }
    reveal: { source: Source }
    select: null
  }>()

  let hits: FileHit[] = []
  let nextAfter: number | null = null
  let indexOn = true
  let indexed: IndexedSource[] = []
  let loading = false
  let error = ''
  let timer: ReturnType<typeof setTimeout> | null = null
  let generation = 0

  $: byId = new Map(sources.map((s) => [s.id, s]))
  $: nameTest = compileNameTest(query, regex)
  $: matchingSources = nameTest ? sources.filter((s) => placeOf(s).some((text) => nameTest?.(text))) : []
  $: notIndexed = indexed.filter((s) => !s.name_index_enabled && byId.has(s.source_id))
  $: oldest = indexed
    .filter((s) => s.name_index_enabled && s.name_indexed_at)
    .map((s) => s.name_indexed_at as string)
    .sort()[0]
  $: schedule(query, regex)

  function compileNameTest(q: string, useRegex: boolean): ((text: string) => boolean) | null {
    const text = q.trim()
    if (!text) return null
    if (useRegex) {
      try {
        const re = new RegExp(text, 'i')
        return (value) => re.test(value)
      } catch {
        return null
      }
    }
    const needle = text.toLowerCase()
    return (value) => value.toLowerCase().includes(needle)
  }

  function placeOf(s: Source): string[] {
    return [s.name, s.host, s.customer_name ?? '', ...s.folder_path.map((f) => f.name)]
  }

  function place(s: Source | undefined): string {
    if (!s) return ''
    return [s.customer_name, ...s.folder_path.map((f) => f.name)].filter(Boolean).join(' / ')
  }

  function schedule(q: string, useRegex: boolean) {
    if (timer) clearTimeout(timer)
    hits = []
    nextAfter = null
    error = ''
    if (!q.trim()) return
    timer = setTimeout(() => load(q, useRegex, 0, true), 250)
  }

  async function load(q: string, useRegex: boolean, after: number, fresh: boolean) {
    const mine = fresh ? ++generation : generation
    loading = true
    try {
      const params = new URLSearchParams({ q: q.trim(), limit: String(PAGE), after: String(after) })
      if (useRegex) params.set('regex', 'true')
      const result = await api.get<FileSearchResult>(`/files?${params}`)
      if (mine !== generation) return
      hits = fresh ? result.hits : [...hits, ...result.hits]
      nextAfter = result.next_after
      indexOn = result.index_enabled
      indexed = result.sources
      error = ''
    } catch (err) {
      if (mine !== generation) return
      error = err instanceof ApiError ? err.detail : 'The search failed'
    } finally {
      if (mine === generation) loading = false
    }
  }

  function fileName(path: string): string {
    return path.slice(path.lastIndexOf('/') + 1)
  }

  function folderOf(path: string): string {
    const cut = path.lastIndexOf('/')
    return cut > 0 ? path.slice(0, cut) : ''
  }

  function open(hit: FileHit) {
    const source = byId.get(hit.source_id)
    dispatch('open', {
      sourceId: hit.source_id,
      sourceName: source?.name ?? `source #${hit.source_id}`,
      path: hit.path,
      member: null,
      name: fileName(hit.path),
    })
  }

  onDestroy(() => {
    if (timer) clearTimeout(timer)
  })
</script>

<div class="search-results">
  {#if matchingSources.length > 0}
    <div class="section-label">Sources</div>
    {#each matchingSources as source (source.id)}
      <button class="row source" on:click={() => dispatch('reveal', { source })} title="Show {source.name} in the tree">
        <span class="name">{source.name}</span>
        <span class="where">{[source.host, place(source)].filter(Boolean).join(' · ')}</span>
      </button>
    {/each}
  {/if}

  <div class="section-label">
    Files
    {#if oldest}
      <span class="as-of" title="File names come from the file-name index, refreshed in the background"
        >· listed {ago(oldest, new Date())}</span
      >
    {/if}
  </div>
  {#if !indexOn}
    <p class="hint">
      The file-name index is turned off (Settings → System).
      <button class="link" on:click={() => dispatch('select', null)}>Select</button> sources or folders to search
      their files live.
    </p>
  {:else if error}
    <p class="error" role="alert">{error}</p>
  {:else}
    {#each hits as hit (`${hit.source_id}:${hit.path}`)}
      {@const source = byId.get(hit.source_id)}
      <button class="row file" on:click={() => open(hit)} title={hit.path}>
        <span class="name">{fileName(hit.path)}</span>
        <span class="size">{formatSize(hit.size)}</span>
        <span class="where"
          >{source?.name ?? `source #${hit.source_id}`}{folderOf(hit.path) ? ` › ${folderOf(hit.path)}` : ''}</span
        >
      </button>
    {/each}
    {#if loading}
      <p class="hint">Searching…</p>
    {:else if hits.length === 0 && matchingSources.length === 0}
      <p class="hint">Nothing matches.</p>
    {:else if hits.length === 0}
      <p class="hint">No file names match.</p>
    {/if}
    {#if nextAfter !== null && !loading}
      <button class="link more" on:click={() => load(query, regex, nextAfter ?? 0, false)}>Show more files</button>
    {/if}
  {/if}

  {#if notIndexed.length > 0}
    <p class="hint not-indexed">
      {notIndexed.length === 1 ? '1 source isn\'t' : `${notIndexed.length} sources aren't`} in the file-name index
      ({notIndexed.map((s) => byId.get(s.source_id)?.name).join(', ')}).
      <button class="link" on:click={() => dispatch('select', null)}>Select</button> them to search their files live.
    </p>
  {/if}
</div>

<style>
  .search-results {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  .section-label {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin: 0.6rem 0 0.2rem;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
  }
  .as-of {
    font-weight: 400;
    text-transform: none;
    letter-spacing: 0;
    color: var(--text-faint);
  }
  .row {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0 0.5rem;
    width: 100%;
    padding: 0.3rem 0.45rem;
    border: none;
    border-radius: 5px;
    background: none;
    color: var(--text);
    text-align: left;
    cursor: pointer;
    font: inherit;
  }
  .row:hover {
    background: var(--border-soft);
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.85rem;
  }
  .size {
    color: var(--text-faint);
    font-size: 0.75rem;
  }
  .where {
    grid-column: 1 / -1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-faint);
    font-size: 0.75rem;
  }
  .hint {
    margin: 0.3rem 0.45rem;
    color: var(--text-faint);
    font-size: 0.8rem;
  }
  .error {
    margin: 0.3rem 0.45rem;
    color: var(--danger);
    font-size: 0.8rem;
  }
  .more {
    margin: 0.3rem 0.45rem;
    font-size: 0.8rem;
    align-self: flex-start;
  }
  .not-indexed {
    margin-top: 0.6rem;
  }
</style>
