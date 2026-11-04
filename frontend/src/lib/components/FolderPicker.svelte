<script lang="ts">
  // Browses folders on a source's host so a Base path can be picked rather
  // than typed (#159): directories only, from POST /sources/pick-folder.
  // The top level is "/" over SSH, the drives over WinRM and the shares
  // over SMB.
  import { createEventDispatcher, onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import type { FolderPickResult, Protocol } from '../types'

  export let protocol: Protocol
  export let host: string
  export let port: number | null
  export let credential: Record<string, string> | null
  export let sourceId: number | null = null
  export let startPath = ''

  const dispatch = createEventDispatcher<{ pick: string; close: void }>()

  let result: FolderPickResult | null = null
  let loading = false
  let error = ''

  async function load(path: string) {
    loading = true
    error = ''
    try {
      result = await api.post<FolderPickResult>('/sources/pick-folder', {
        ...(sourceId !== null ? { source_id: sourceId } : { protocol }),
        host,
        port,
        ...(credential ? { credential } : {}),
        path,
      })
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Could not list folders'
    } finally {
      loading = false
    }
  }

  onMount(async () => {
    await load(startPath)
    // A typed path that doesn't exist (yet): start from the top instead,
    // keeping the error visible above the listing.
    if (!result && startPath) {
      const firstError = error
      await load('')
      if (!error) error = firstError
    }
  })

  // The SMB share list and the WinRM drive list are where you choose a
  // share or drive, not a folder you can use as is (on SMB).
  $: canUse = result !== null && !(result.kind === 'shares')
  $: heading =
    result?.kind === 'shares' ? 'Shares' : result?.kind === 'drives' ? 'Drives' : result?.path || '/'
</script>

<div class="picker" role="dialog" aria-label="Pick a folder">
  <div class="bar">
    <button
      type="button"
      class="btn btn-ghost small"
      on:click={() => result?.parent !== null && result && load(result.parent ?? '')}
      disabled={loading || !result || result.parent === null}
      aria-label="Up one level">↑ Up</button
    >
    <span class="where mono" title={heading}>{heading}</span>
    <button type="button" class="btn btn-ghost small" on:click={() => dispatch('close')}
      >Cancel</button
    >
  </div>

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if loading}
    <p class="hint">Loading…</p>
  {:else if result}
    {#if result.entries.length === 0}
      <p class="hint">
        {result.kind === 'shares'
          ? 'No shares this account can see.'
          : 'No folders here.'}
      </p>
    {:else}
      <ul>
        {#each result.entries as entry (entry.path)}
          <li>
            <button type="button" class="entry" on:click={() => load(entry.path)}>
              <span aria-hidden="true">📁</span>
              {entry.name}
            </button>
          </li>
        {/each}
      </ul>
      {#if result.truncated}
        <p class="hint">Only the first {result.entries.length} folders are shown.</p>
      {/if}
    {/if}
    <div class="actions">
      <button
        type="button"
        class="btn btn-primary small"
        disabled={!canUse || !result.path}
        on:click={() => result && dispatch('pick', result.path)}
      >
        Use {result.path || 'this folder'}
      </button>
    </div>
  {/if}
</div>

<style>
  .picker {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    padding: 0.6rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .where {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
    font-size: 0.82rem;
  }
  .mono {
    font-family: var(--font-mono);
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 240px;
    overflow: auto;
  }
  .entry {
    width: 100%;
    display: flex;
    gap: 0.4rem;
    align-items: center;
    background: none;
    border: none;
    color: var(--text);
    text-align: left;
    padding: 0.3rem 0.4rem;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 0.85rem;
  }
  .entry:hover {
    background: var(--bg-elevated);
  }
  .actions {
    display: flex;
    justify-content: flex-end;
  }
  .small {
    padding: 0.3rem 0.6rem;
    font-size: 0.8rem;
  }
  .error {
    color: var(--danger);
    margin: 0;
    font-size: 0.82rem;
  }
  .hint {
    color: var(--text-faint);
    margin: 0;
    font-size: 0.82rem;
  }
</style>
