<script lang="ts">
  // A table header that sorts its list (#238): click to sort ascending,
  // again to flip. aria-sort tells screen readers the current order.
  import { createEventDispatcher } from 'svelte'
  import type { SortState } from '../table-view'

  export let label: string
  export let key: string
  export let sort: SortState | null

  const dispatch = createEventDispatcher<{ sort: string }>()

  $: active = sort?.key === key
  $: ariaSort = (active ? (sort?.dir === 'asc' ? 'ascending' : 'descending') : 'none') as
    | 'ascending'
    | 'descending'
    | 'none'
</script>

<th aria-sort={ariaSort}>
  <button type="button" class="sort" class:active on:click={() => dispatch('sort', key)}>
    {label}
    <span class="arrow" aria-hidden="true">{active ? (sort?.dir === 'asc' ? '▲' : '▼') : '↕'}</span>
  </button>
</th>

<style>
  .sort {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0;
    border: none;
    background: none;
    color: inherit;
    font: inherit;
    text-transform: inherit;
    letter-spacing: inherit;
    cursor: pointer;
  }
  .sort:hover,
  .sort.active {
    color: var(--text);
  }
  .arrow {
    font-size: 0.65rem;
    color: var(--text-faint);
  }
  .sort.active .arrow {
    color: var(--accent);
  }
</style>
