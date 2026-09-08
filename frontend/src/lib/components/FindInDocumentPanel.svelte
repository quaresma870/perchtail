<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte'
  import { findAllMatches, snippetAround } from '../find-in-document'

  export let content = ''

  let query = ''
  let caseSensitive = false
  let useRegex = false
  let queryInput: HTMLInputElement

  const dispatch = createEventDispatcher<{ jump: { line: number }; close: void }>()

  onMount(() => queryInput?.focus())

  $: result = findAllMatches(content, query, { caseSensitive, useRegex })
  $: rows = result.matches.map((m) => ({
    line: m.line,
    ...snippetAround(m.lineText, m.matchStart, m.matchLength),
  }))
</script>

<div class="find-all-panel">
  <div class="find-all-header">
    <input
      class="input"
      type="text"
      placeholder="Find all in this file…"
      bind:value={query}
      bind:this={queryInput}
    />
    <label class="option">
      <input type="checkbox" bind:checked={caseSensitive} />
      Match case
    </label>
    <label class="option">
      <input type="checkbox" bind:checked={useRegex} />
      Regex
    </label>
    <span class="count">
      {#if !query}
        &nbsp;
      {:else if result.matches.length === 0}
        No matches
      {:else}
        {result.matches.length}{result.truncated ? '+' : ''} match{result.matches.length === 1 ? '' : 'es'}
      {/if}
    </span>
    <button class="close" on:click={() => dispatch('close')} aria-label="Close find all">×</button>
  </div>

  <div class="find-all-results">
    {#each rows as row, i (i)}
      <button class="result-row" on:click={() => dispatch('jump', { line: row.line })}>
        <span class="line-number">{row.line}</span>
        <span class="snippet"
          >{row.text.slice(0, row.matchStart)}<mark>{row.text.slice(
            row.matchStart,
            row.matchStart + row.matchLength,
          )}</mark>{row.text.slice(row.matchStart + row.matchLength)}</span
        >
      </button>
    {/each}
    {#if result.truncated}
      <p class="hint">Showing the first {result.matches.length} matches — narrow the search to see fewer.</p>
    {/if}
  </div>
</div>

<style>
  .find-all-panel {
    flex: 0 0 240px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-top: 1px solid var(--border-soft);
    background: var(--bg-elevated);
  }
  .find-all-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .find-all-header .input {
    flex: 1;
    min-width: 0;
  }
  .option {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.78rem;
    color: var(--text-muted);
    white-space: nowrap;
  }
  .count {
    font-size: 0.78rem;
    color: var(--text-faint);
    white-space: nowrap;
    min-width: 5rem;
    text-align: right;
  }
  .close {
    border: none;
    background: none;
    color: var(--text-faint);
    cursor: pointer;
    font-size: 0.9rem;
    padding: 0 0.2rem;
  }
  .close:hover {
    color: var(--danger);
  }
  .find-all-results {
    flex: 1;
    overflow: auto;
  }
  .result-row {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    width: 100%;
    border: none;
    background: none;
    padding: 0.3rem 0.75rem;
    text-align: left;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }
  .result-row:hover {
    background: var(--bg-hover);
  }
  .line-number {
    flex: 0 0 auto;
    color: var(--text-faint);
    min-width: 3ch;
    text-align: right;
  }
  .snippet {
    color: var(--text-muted);
    white-space: pre;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .snippet mark {
    background: rgba(251, 191, 36, 0.35);
    color: var(--text);
    border-radius: 2px;
  }
  .hint {
    padding: 0.5rem 0.75rem;
    font-size: 0.78rem;
    color: var(--text-faint);
    margin: 0;
  }
</style>
