<script lang="ts">
  import { createEventDispatcher, onMount, tick } from 'svelte'
  import {
    compileFind,
    copyText,
    matchingLines,
    refineLines,
    rowView,
    shownLength,
    type FindSpec,
    type RefineStep,
    type RowView,
  } from '../find-in-document'
  import { clampPanelHeight, KEY_STEP_PX, loadPanelHeight, MIN_PANEL_PX, savePanelHeight } from '../panel-size'

  /** Notepad++-style "Find All" (#155): one row per matching line with the
   * whole line shown and every match highlighted; rows can be selected
   * (click, Ctrl/Cmd+click, Shift+click, Ctrl/Cmd+A) and a right-click menu
   * copies them, narrows the results further (search in results / exclude
   * matching, chainable with a way back), or jumps to the line. Purely
   * client-side over the already-fetched content. The search itself comes
   * from the find bar (#299). */
  export let content = ''
  export let spec: FindSpec

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

  onMount(() => resultsEl?.focus())

  // Resizable (#285): dragging the handle on top, or its arrow keys, sets
  // the height; null keeps the default share of the pane.
  let panelEl: HTMLDivElement
  let heightPx: number | null = loadPanelHeight()
  let dragging = false

  function availableHeight(): number {
    // The panel and the editor above it share the pane.
    return panelEl?.parentElement?.clientHeight ?? window.innerHeight
  }

  function setHeight(px: number) {
    heightPx = clampPanelHeight(px, availableHeight())
  }

  function startDrag(event: PointerEvent) {
    event.preventDefault()
    dragging = true
    const startY = event.clientY
    const startHeight = panelEl.getBoundingClientRect().height
    const handle = event.currentTarget as HTMLElement
    handle.setPointerCapture(event.pointerId)
    const move = (e: PointerEvent) => setHeight(startHeight + (startY - e.clientY))
    const stop = () => {
      dragging = false
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', stop)
      handle.removeEventListener('pointercancel', stop)
      if (heightPx !== null) savePanelHeight(heightPx)
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', stop)
    handle.addEventListener('pointercancel', stop)
  }

  function resizeByKey(event: KeyboardEvent) {
    const step = event.key === 'ArrowUp' ? KEY_STEP_PX : event.key === 'ArrowDown' ? -KEY_STEP_PX : 0
    if (!step) return
    event.preventDefault()
    setHeight(panelEl.getBoundingClientRect().height + step)
    if (heightPx !== null) savePanelHeight(heightPx)
  }

  // The file's lines, split once per content; the results are line numbers
  // (1-based) into it -- all of them, with no limit.
  $: fileLines = content.split('\n')
  $: pattern = compileFind(spec.query, spec)
  $: baseLines = matchingLines(fileLines, pattern)
  $: lines = steps.reduce((acc, step) => refineLines(fileLines, acc, step), baseLines)
  // What each drawn row shows, worked out once per row and search.
  let views = new Map<number, RowView>()
  $: fileLines, pattern, (views = new Map())
  function view(line: number): RowView {
    let v = views.get(line)
    if (!v) {
      v = rowView(fileLines[line - 1], pattern!)
      views.set(line, v)
    }
    return v
  }
  // A new search starts a new result set: refinements and selection reset.
  $: spec, resetRefinements()

  function resetRefinements() {
    steps = []
    selected = new Set()
    anchor = null
    toTop()
  }

  // Only the rows in view are drawn, with no limit on how many there are:
  // thousands of full log lines (some tens of thousands of characters long)
  // made a list too big for the browser to paint while scrolling.
  const ROW_PX = 22
  const OVERSCAN = 12
  let scrollTop = 0
  let viewHeight = 0
  $: firstShown = Math.max(0, Math.floor(scrollTop / ROW_PX) - OVERSCAN)
  $: lastShown = Math.min(lines.length, Math.ceil((scrollTop + viewHeight) / ROW_PX) + OVERSCAN)
  $: shown = lines.slice(firstShown, lastShown)
  $: widestChars = lines.reduce((max, n) => Math.max(max, shownLength(fileLines[n - 1])), 0)

  function toTop() {
    scrollTop = 0
    if (resultsEl) resultsEl.scrollTop = 0
  }

  function select(row: number, event: MouseEvent) {
    const next = new Set(selected)
    if (event.shiftKey && anchor !== null) {
      const from = lines.indexOf(anchor)
      const to = lines.indexOf(row)
      const [a, b] = from < to ? [from, to] : [to, from]
      if (!(event.ctrlKey || event.metaKey)) next.clear()
      for (const n of lines.slice(a, b + 1)) next.add(n)
    } else if (event.ctrlKey || event.metaKey) {
      if (next.has(row)) next.delete(row)
      else next.add(row)
      anchor = row
    } else {
      next.clear()
      next.add(row)
      anchor = row
    }
    selected = next
  }

  function selectAll() {
    selected = new Set(lines)
    menu = null
  }

  function openMenu(row: number | null, event: MouseEvent) {
    event.preventDefault()
    if (row !== null && !selected.has(row)) {
      selected = new Set([row])
      anchor = row
    }
    menu = { x: event.clientX, y: event.clientY }
  }

  async function copy(withNumbers: boolean) {
    menu = null
    const text = copyText(fileLines, lines, selected, withNumbers)
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
      steps = [
        ...steps,
        {
          query: refineQuery.trim(),
          caseSensitive: spec.caseSensitive,
          useRegex: spec.useRegex,
          exclude: refining.exclude,
        },
      ]
      selected = new Set()
      anchor = null
      toTop()
    }
    refining = null
  }

  function back(toIndex: number) {
    steps = steps.slice(0, toIndex)
    selected = new Set()
    toTop()
  }

  function goToSelected() {
    menu = null
    const first = lines.find((n) => selected.has(n))
    if (first) dispatch('jump', { line: first })
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

<div
  class="find-all-panel"
  class:dragging
  bind:this={panelEl}
  style={heightPx !== null ? `height: ${heightPx}px` : undefined}
>
  <!-- svelte-ignore a11y-no-noninteractive-tabindex a11y-no-noninteractive-element-interactions -->
  <div
    class="resize-handle"
    role="separator"
    aria-orientation="horizontal"
    aria-label="Resize find all"
    aria-valuemin={MIN_PANEL_PX}
    aria-valuenow={heightPx ?? undefined}
    title="Drag to resize"
    tabindex="0"
    on:pointerdown={startDrag}
    on:keydown={resizeByKey}
  ></div>
  <div class="find-all-header">
    <span class="title"
      >Find all <q>{spec.query}</q>{#if spec.caseSensitive || spec.wholeWord || spec.useRegex}<span class="options"
          >{[spec.caseSensitive && 'match case', spec.wholeWord && 'whole word', spec.useRegex && 'regex']
            .filter(Boolean)
            .join(', ')}</span
        >{/if}</span
    >
    <span class="count">
      {#if lines.length === 0}
        No matches
      {:else}
        {lines.length.toLocaleString()} line{lines.length === 1 ? '' : 's'}
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
    on:scroll={() => (scrollTop = resultsEl.scrollTop)}
    bind:clientHeight={viewHeight}
  >
    <div
      class="rows"
      style="height: {lines.length * ROW_PX}px; min-width: calc({widestChars}ch + 12ch)"
    >
      <div class="rows-window" style="transform: translateY({firstShown * ROW_PX}px)">
        {#each shown as row, i (row)}
          {@const v = view(row)}
          <div
            class="result-row"
            class:selected={selected.has(row)}
            style="height: {ROW_PX}px"
            role="option"
            aria-selected={selected.has(row)}
            aria-setsize={lines.length}
            aria-posinset={firstShown + i + 1}
            tabindex="-1"
            on:click={(e) => select(row, e)}
            on:dblclick={() => dispatch('jump', { line: row })}
            on:contextmenu|stopPropagation={(e) => openMenu(row, e)}
            on:keydown={() => {}}
          >
            <span class="line-number">{row}</span>
            <span class="line-text"
              >{#if v.before}<span class="clip" title="Double-click to see the whole line">…</span>{/if}{#each v.segments as seg}{#if seg.hit}<mark>{seg.text}</mark>{:else}{seg.text}{/if}{/each}{#if v.after}<span class="clip" title="Double-click to see the whole line">…</span>{/if}</span
            >
            {#if v.hits > 1}
              <span class="hits">{v.hits}×</span>
            {/if}
          </div>
        {/each}
      </div>
    </div>
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
  .find-all-panel.dragging {
    user-select: none;
  }
  .resize-handle {
    position: absolute;
    top: -4px;
    left: 0;
    right: 0;
    height: 8px;
    cursor: row-resize;
    z-index: 2;
    touch-action: none;
  }
  .resize-handle:hover,
  .resize-handle:focus-visible,
  .find-all-panel.dragging .resize-handle {
    background: linear-gradient(transparent 3px, var(--accent-border) 3px, var(--accent-border) 5px, transparent 5px);
    outline: none;
  }
  .find-all-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .title q {
    color: var(--text);
    font-family: var(--font-mono);
  }
  .options {
    margin-left: 0.5rem;
    color: var(--text-faint);
    font-size: 0.74rem;
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
  .rows {
    position: relative;
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }
  .rows-window {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
  }
  .result-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0 0.75rem;
    box-sizing: border-box;
    cursor: default;
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
  .clip {
    color: var(--text-faint);
  }
  .hits {
    flex: 0 0 auto;
    color: var(--text-faint);
    font-size: 0.72rem;
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
