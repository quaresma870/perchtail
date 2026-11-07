<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte'
  import type { FindSpec } from '../find-in-document'

  /** One find bar for Find and Find all (#299): the text and its options,
   * previous/next with "3 of 120", and Find all, which lists the matching
   * lines in the panel below. Enter goes to the next match, Shift+Enter to
   * the previous one, Alt+Enter runs Find all, Escape closes the bar. */
  export let spec: FindSpec
  /** Matches counted so far, and whether the count is complete. */
  export let total = 0
  export let counting = false
  /** The current match's position, or 0 before moving to one. */
  export let current = 0
  /** The query doesn't compile (an invalid regex). */
  export let invalid = false

  const dispatch = createEventDispatcher<{ next: void; previous: void; findall: void; close: void }>()
  let input: HTMLInputElement

  export async function focus(select = true) {
    await tick()
    input?.focus()
    if (select) input?.select()
  }

  function toggle(option: 'caseSensitive' | 'useRegex' | 'wholeWord') {
    spec = { ...spec, [option]: !spec[option] }
  }

  function onKeydown(event: KeyboardEvent) {
    // Enter on the text box only: on a button it presses that button.
    if (event.key === 'Enter' && event.target === input) {
      event.preventDefault()
      if (event.altKey) dispatch('findall')
      else dispatch(event.shiftKey ? 'previous' : 'next')
    } else if (event.key === 'Escape') {
      event.preventDefault()
      dispatch('close')
    }
  }

  $: status = !spec.query
    ? ''
    : invalid
      ? 'Invalid regex'
      : total === 0 && !counting
        ? 'No matches'
        : current > 0 && !counting
          ? `${current.toLocaleString()} of ${total.toLocaleString()}`
          : `${total.toLocaleString()}${counting ? '+' : ''} ${total === 1 && !counting ? 'match' : 'matches'}`
</script>

<!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
<div class="find-bar" role="search" aria-label="Find in this file" on:keydown={onKeydown}>
  <input
    class="input find-input"
    type="text"
    placeholder="Find"
    aria-label="Find"
    spellcheck="false"
    bind:this={input}
    value={spec.query}
    on:input={(e) => (spec = { ...spec, query: e.currentTarget.value })}
  />
  <button
    class="opt"
    class:active={spec.caseSensitive}
    aria-pressed={spec.caseSensitive}
    title="Match case"
    aria-label="Match case"
    on:click={() => toggle('caseSensitive')}>Aa</button
  >
  <button
    class="opt"
    class:active={spec.wholeWord}
    aria-pressed={spec.wholeWord}
    title="Whole word"
    aria-label="Whole word"
    on:click={() => toggle('wholeWord')}><span class="ww">ab</span></button
  >
  <button
    class="opt mono"
    class:active={spec.useRegex}
    aria-pressed={spec.useRegex}
    title="Regex"
    aria-label="Regex"
    on:click={() => toggle('useRegex')}>.*</button
  >
  <span class="status" class:invalid role="status" aria-live="polite">{status}</span>
  <button
    class="nav"
    title="Previous match (Shift+Enter)"
    aria-label="Previous match"
    disabled={!spec.query || invalid}
    on:click={() => dispatch('previous')}
  >
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"
      ><path d="M4 10l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg
    >
  </button>
  <button
    class="nav"
    title="Next match (Enter)"
    aria-label="Next match"
    disabled={!spec.query || invalid}
    on:click={() => dispatch('next')}
  >
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"
      ><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg
    >
  </button>
  <button
    class="btn-findall"
    title="List every matching line below (Alt+Enter)"
    disabled={!spec.query || invalid}
    on:click={() => dispatch('findall')}>Find all</button
  >
  <button class="nav close" title="Close (Escape)" aria-label="Close find" on:click={() => dispatch('close')}>×</button>
</div>

<style>
  .find-bar {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.3rem 0.6rem;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
    flex-wrap: wrap;
  }
  .find-input {
    flex: 0 1 22rem;
    min-width: 10rem;
    padding: 0.25rem 0.5rem;
    font-size: 0.82rem;
  }
  .opt,
  .nav,
  .btn-findall {
    display: inline-grid;
    place-items: center;
    height: 1.65rem;
    min-width: 1.65rem;
    padding: 0 0.35rem;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--text-muted);
    font: inherit;
    font-size: 0.78rem;
    cursor: pointer;
  }
  .opt:hover,
  .nav:hover:not(:disabled),
  .btn-findall:hover:not(:disabled) {
    background: var(--bg-hover);
    color: var(--text);
  }
  .opt.active {
    background: var(--accent-soft);
    border-color: var(--accent-border);
    color: var(--text);
  }
  .opt:focus-visible,
  .nav:focus-visible,
  .btn-findall:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .mono {
    font-family: var(--font-mono);
  }
  .ww {
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .nav:disabled,
  .btn-findall:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .btn-findall {
    padding: 0 0.6rem;
    border-color: var(--border);
    color: var(--text);
  }
  .status {
    min-width: 7rem;
    padding: 0 0.4rem;
    font-size: 0.76rem;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .status.invalid {
    color: var(--danger);
  }
  .close {
    margin-left: auto;
    font-size: 1rem;
  }
</style>
