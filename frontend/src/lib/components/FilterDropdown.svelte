<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte'

  /** A filter dropdown (#358): a button that shows what's picked, opening a
   * panel with a search box on top and a checkbox per option. Several can be
   * picked; none means "no filter". Closes on Escape or a click outside. */
  export let label: string
  export let options: { value: string; label: string; mono?: boolean }[]
  export let selected: Set<string>
  export let noneLabel = 'All'

  const dispatch = createEventDispatcher<{ change: Set<string> }>()

  let open = false
  let query = ''
  let root: HTMLElement
  let searchInput: HTMLInputElement

  $: shown = query.trim()
    ? options.filter((o) => o.label.toLowerCase().includes(query.trim().toLowerCase()))
    : options
  $: summary =
    selected.size === 0
      ? noneLabel
      : selected.size === 1
        ? (options.find((o) => selected.has(o.value))?.label ?? '1 selected')
        : `${selected.size} selected`

  async function toggleOpen() {
    open = !open
    if (open) {
      query = ''
      await tick()
      searchInput?.focus()
    }
  }

  function toggle(value: string) {
    const next = new Set(selected)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    dispatch('change', next)
  }

  function clear() {
    dispatch('change', new Set<string>())
  }

  function onWindow(event: MouseEvent | KeyboardEvent) {
    if (!open) return
    if (event instanceof KeyboardEvent) {
      if (event.key === 'Escape') open = false
      return
    }
    if (root && !root.contains(event.target as Node)) open = false
  }
</script>

<svelte:window on:click={onWindow} on:keydown={onWindow} />

<div class="dropdown" bind:this={root}>
  <button
    type="button"
    class="input dropdown-toggle"
    class:active={selected.size > 0}
    aria-haspopup="true"
    aria-expanded={open}
    aria-label="{label}: {summary}"
    on:click={toggleOpen}
  >
    <span class="summary">{summary}</span>
    <span class="caret">▾</span>
  </button>
  {#if open}
    <div class="dropdown-panel" role="group" aria-label={label}>
      <input
        class="input dropdown-search"
        type="search"
        placeholder="Search {label.toLowerCase()}…"
        aria-label="Search {label.toLowerCase()}"
        bind:this={searchInput}
        bind:value={query}
      />
      <div class="dropdown-list">
        {#each shown as option (option.value)}
          <label class="dropdown-item" class:mono={option.mono}>
            <input type="checkbox" checked={selected.has(option.value)} on:change={() => toggle(option.value)} />
            <span>{option.label}</span>
          </label>
        {:else}
          <p class="dropdown-empty">No match</p>
        {/each}
      </div>
      {#if selected.size > 0}
        <button type="button" class="link dropdown-clear" on:click={clear}>{noneLabel}</button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .dropdown {
    position: relative;
  }
  .dropdown-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    min-width: 180px;
    max-width: 280px;
    cursor: pointer;
    text-align: left;
  }
  .dropdown-toggle.active {
    border-color: var(--accent);
  }
  .summary {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .caret {
    color: var(--text-faint);
    font-size: 0.75rem;
  }
  .dropdown-panel {
    position: absolute;
    z-index: 20;
    top: calc(100% + 4px);
    left: 0;
    min-width: max(100%, 240px);
    padding: 0.4rem;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: var(--shadow-card);
  }
  .dropdown-search {
    width: 100%;
    margin-bottom: 0.3rem;
    font-size: 0.8rem;
  }
  .dropdown-list {
    max-height: 280px;
    overflow-y: auto;
  }
  .dropdown-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.45rem;
    border-radius: 5px;
    font-size: 0.82rem;
    color: var(--text);
    white-space: nowrap;
    cursor: pointer;
  }
  .dropdown-item.mono {
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }
  .dropdown-item:hover {
    background: var(--border-soft);
  }
  .dropdown-empty {
    margin: 0.3rem 0.45rem;
    font-size: 0.8rem;
    color: var(--text-faint);
  }
  .dropdown-clear {
    margin: 0.35rem 0.45rem 0.1rem;
    font-size: 0.78rem;
  }
</style>
