<script lang="ts">
  // Line icons for the viewer toolbar (#286), drawn like the rest of the
  // app's inline icons: 24x24, stroke currentColor.
  export let name:
    | 'copy'
    | 'bookmark'
    | 'chevron-up'
    | 'chevron-down'
    | 'alert'
    | 'refresh'
    | 'download'
</script>

<svg
  class="toolbar-icon"
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.8"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {#if name === 'copy'}
    <rect x="8" y="8" width="12" height="12" rx="2" />
    <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
  {:else if name === 'bookmark'}
    <path d="M7 4h10a1 1 0 0 1 1 1v15l-6-4-6 4V5a1 1 0 0 1 1-1z" />
  {:else if name === 'chevron-up'}
    <path d="m6 15 6-6 6 6" />
  {:else if name === 'chevron-down'}
    <path d="m6 9 6 6 6-6" />
  {:else if name === 'alert'}
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
    <path d="M10.3 4.3 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0z" />
  {:else if name === 'refresh'}
    <path d="M20 11a8 8 0 1 0-2.3 5.7" />
    <path d="M20 4v7h-7" />
  {:else if name === 'download'}
    <path d="M12 3v12" />
    <path d="m6 11 6 6 6-6" />
    <path d="M5 21h14" />
  {/if}
</svg>

<style>
  .toolbar-icon {
    width: 15px;
    height: 15px;
    display: block;
  }
</style>
