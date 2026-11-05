<script lang="ts">
  import { createEventDispatcher, onMount, tick } from 'svelte'
  import {
    copyText,
    findAllMatches,
    groupMatchesByLine,
    refineLines,
    segments,
    type LineResult,
    type RefineStep,
  } from '../find-in-document'

  /** Notepad++-style "Find All" (#155): one row per matching line with the
   * whole line shown and every match highlighted; rows can be selected
   * (click, Ctrl/Cmd+click, Shift+click, Ctrl/Cmd+A) and a right-click menu
   * copies them, narrows the results further (search in results / exclude
   * matching, chainable with a way back), or jumps to the line. Purely
   * client-side over the already-fetched content. */
  export let content = ''

  let query = ''
  let caseSensitive = false
  let useRegex = false
  let queryInput: HTMLInputElement
  let resultsEl: HTMLDivElement

  let steps: RefineStep[] = []
  let selected = new Set<number>()
  let anchor: number | null = null

  let menu: { x: number; y: number } | null = null
  let refining: { exclude: boolean } | null = null
  let refineQuery = ''
  let refineInput: HTMLInputElement
  let copyStatus = ''

  const dispatch = createEventDispatcher<{ jump: { line: number }; close: void }>()

  onMount(() => queryInput?.focus())

  $: result = findAllMatches(content, query, { caseSensitive, useRegex })
  $: baseLines = groupMatchesByLine(result.matches)
  $: lines = steps.reduce((acc, step) => refineLines(acc, step), baseLines)
  // A new search starts a new result set: refinements and selection reset.
  $: query, caseSensitive, useRegex, resetRefinements()

  function resetRefinements() {
    steps = []
    selected = new Set()
    anchor = null
  }

  function select(row: LineResult, event: MouseEvent) {
    const next = new Set(selected)
    if (event.shiftKey && anchor !== null) {
      const from = lines.findIndex((l) => l.line === anchor)
      const to = lines.findIndex((l) => l.line === row.line)
      const [a, b] = from < to ? [from, to] : [to, from]
      if (!(event.ctrlKey || event.metaKey)) next.clear()
      for (const l of lines.slice(a, b + 1)) next.add(l.line)
    } else if (event.ctrlKey || event.metaKey) {
      if (next.has(row.line)) next.delete(row.line)
      else next.add(row.line)
      anchor = row.line
    } else {
      next.clear()
      next.add(row.line)
      anchor = row.line
    }
    selected = next
  }

  function selectAll() {
    selected = new Set(lines.map((l) => l.line))
    menu = null
  }

  function openMenu(row: LineResult | null, event: MouseEvent) {
    event.preventDefault()
    if (row && !selected.has(row.line)) {
      selected = new Set([row.line])
      anchor = row.line
    }
    menu = { x: event.clientX, y: event.clientY }
  }

  async function copy(withNumbers: boolean) {
    menu = null
    const text = copyText(lines, selected, withNumbers)
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      copyStatus = `Copied ${selected.size} line${selected.size === 1 ? '' : 's'}`
    } catch {
      copyStatus = 'Copy needs a secure page (HTTPS or localhost)'
    }
    setTimeout(() => (copyStatus = ''), 2000)
  }

  async function startRefine(exclude: boolean) {
    menu = null
    refining = { exclude }
    refineQuery = ''
    await tick()
    refineInput?.focus()
  }

  function applyRefine() {
    if (refining && refineQuery.trim()) {
      steps = [...steps, { query: refineQuery.trim(), caseSensitive, useRegex, exclude: refining.exclude }]
      selected = new Set()
      anchor = null
    }
    refining = null
  }

  function back(toIndex: number) {
    steps = steps.slice(0, toIndex)
    selected = new Set()
  }

  function goToSelected() {
    menu = null
    const first = lines.find((l) => selected.has(l.line))
    if (first) dispatch('jump', { line: first.line })
  }

  function onKeydown(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
      event.preventDefault()
      selectAll()
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c' && selected.size) {
      event.preventDefault()
      copy(false)
    } else if (event.key === 'Enter' && selected.size) {
      goToSelected()
    } else if (event.key === 'Escape') {
      menu = null
    }
  }
</script>

<svelte:window on:click={() => (menu = null)} />

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
      {:else if lines.length === 0}
        No matches
      {:else}
        {lines.length}{result.truncated ? '+' : ''} line{lines.length === 1 ? '' : 's'}
      {/if}
    </span>
    <button class="close" on:click={() => dispatch('close')} aria-label="Close find all">×</button>
  </div>

  {#if steps.length > 0 || refining || copyStatus}
    <div class="refine-bar">
      {#if steps.length > 0}
        <button class="crumb" on:click={() => back(0)} title="Back to all results">All results</button>
        {#each steps as step, i}
          <span class="sep">›</span>
          <button class="crumb" on:click={() => back(i + 1)} title="Back to this step">
            {step.exclude ? 'without' : 'with'} “{step.query}”
          </button>
        {/each}
      {/if}
      {#if refining}
        <form class="refine-form" on:submit|preventDefault={applyRefine}>
          <input
            class="input"
            bind:this={refineInput}
            bind:value={refineQuery}
            placeholder={refining.exclude ? 'Exclude lines matching…' : 'Search in these results…'}
            on:keydown={(e) => e.key === 'Escape' && (refining = null)}
          />
        </form>
      {/if}
      {#if copyStatus}
        <span class="copy-status">{copyStatus}</span>
      {/if}
    </div>
  {/if}

  <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
  <div
    class="find-all-results"
    bind:this={resultsEl}
    tabindex="0"
    role="listbox"
    aria-multiselectable="true"
    on:keydown={onKeydown}
    on:contextmenu={(e) => openMenu(null, e)}
  >
    {#each lines as row (row.line)}
      <div
        class="result-row"
        class:selected={selected.has(row.line)}
        role="option"
        aria-selected={selected.has(row.line)}
        tabindex="-1"
        on:click={(e) => select(row, e)}
        on:dblclick={() => dispatch('jump', { line: row.line })}
        on:contextmenu|stopPropagation={(e) => openMenu(row, e)}
        on:keydown={() => {}}
      >
        <span class="line-number">{row.line}</span>
        <span class="line-text"
          >{#each segments(row) as seg}{#if seg.hit}<mark>{seg.text}</mark>{:else}{seg.text}{/if}{/each}</span
        >
        {#if row.ranges.length > 1}
          <span class="hits">{row.ranges.length}×</span>
        {/if}
      </div>
    {/each}
    {#if result.truncated}
      <p class="hint">Showing the first {result.matches.length} matches — narrow the search to see fewer.</p>
    {/if}
  </div>

  {#if menu}
    <ul class="context-menu" style="left: {menu.x}px; top: {menu.y}px" role="menu">
      <li><button disabled={!selected.size} on:click={() => copy(false)}>Copy selected lines</button></li>
      <li>
        <button disabled={!selected.size} on:click={() => copy(true)}>Copy with line numbers</button>
      </li>
      <li><button on:click={selectAll}>Select all</button></li>
      <li class="divider"></li>
      <li><button disabled={!lines.length} on:click={() => startRefine(false)}>Search in these results…</button></li>
      <li><button disabled={!lines.length} on:click={() => startRefine(true)}>Exclude lines matching…</button></li>
      <li class="divider"></li>
      <li><button disabled={!selected.size} on:click={goToSelected}>Go to line</button></li>
      <li><button on:click={() => { menu = null; dispatch('close') }}>Close</button></li>
    </ul>
  {/if}
</div>

<style>
  .find-all-panel {
    flex: 0 0 auto;
    height: 35%;
    min-height: 160px;
    display: flex;
    flex-direction: column;
    border-top: 1px solid var(--border-soft);
    background: var(--bg-elevated);
    position: relative;
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
  .option input {
    width: auto;
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
  .refine-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
    padding: 0.35rem 0.75rem;
    border-bottom: 1px solid var(--border-soft);
    font-size: 0.76rem;
  }
  .crumb {
    border: 1px solid var(--border);
    background: none;
    color: var(--text-muted);
    border-radius: 999px;
    padding: 0.05rem 0.55rem;
    cursor: pointer;
    font-size: 0.74rem;
  }
  .crumb:hover {
    color: var(--text);
    border-color: var(--accent-border);
  }
  .sep {
    color: var(--text-faint);
  }
  .refine-form .input {
    width: 16rem;
    padding: 0.15rem 0.5rem;
    font-size: 0.76rem;
  }
  .copy-status {
    color: var(--text-faint);
    font-style: italic;
  }
  .find-all-results {
    flex: 1;
    min-height: 0;
    overflow: auto;
    outline: none;
  }
  .result-row {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    padding: 0.2rem 0.75rem;
    cursor: default;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    user-select: none;
  }
  .result-row:hover {
    background: var(--bg-hover);
  }
  .result-row.selected {
    background: var(--accent-soft);
  }
  .line-number {
    flex: 0 0 auto;
    color: var(--text-faint);
    min-width: 4ch;
    text-align: right;
  }
  .line-text {
    color: var(--text-muted);
    white-space: pre;
  }
  .line-text mark {
    background: rgba(251, 191, 36, 0.35);
    color: var(--text);
    border-radius: 2px;
  }
  .hits {
    flex: 0 0 auto;
    color: var(--text-faint);
    font-size: 0.72rem;
  }
  .hint {
    padding: 0.5rem 0.75rem;
    font-size: 0.78rem;
    color: var(--text-faint);
    margin: 0;
  }
  .context-menu {
    position: fixed;
    z-index: 50;
    list-style: none;
    margin: 0;
    padding: 0.25rem 0;
    min-width: 14rem;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  }
  .context-menu button {
    display: block;
    width: 100%;
    text-align: left;
    border: none;
    background: none;
    padding: 0.35rem 0.85rem;
    font-size: 0.8rem;
    color: var(--text);
    cursor: pointer;
  }
  .context-menu button:hover:not(:disabled) {
    background: var(--bg-hover);
  }
  .context-menu button:disabled {
    color: var(--text-faint);
    cursor: default;
  }
  .divider {
    height: 1px;
    margin: 0.25rem 0;
    background: var(--border-soft);
  }
</style>
