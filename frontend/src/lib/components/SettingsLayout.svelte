<script lang="ts">
  import SettingsNav from './SettingsNav.svelte'

  // Every Settings page: the sidebar on the left, the page on the right.
  // `nav` off leaves only the page (an editor shown inside a popup).
  export let nav = true
</script>

{#if nav}
  <div class="settings-layout">
    <div class="settings-sidebar">
      <SettingsNav />
    </div>
    <div class="settings-content">
      <slot />
    </div>
  </div>
{:else}
  <slot />
{/if}

<style>
  .settings-layout {
    display: flex;
    flex: 1 0 auto;
    align-items: stretch;
    min-width: 0;
  }
  .settings-sidebar {
    flex: 0 0 14rem;
    width: 14rem;
    border-right: 1px solid var(--border-soft);
    background: var(--bg-elevated);
  }
  .settings-sidebar :global(.settings-nav) {
    position: sticky;
    top: 0;
  }
  .settings-content {
    flex: 1 1 auto;
    min-width: 0;
  }

  @media (max-width: 767.98px) {
    .settings-layout {
      flex-direction: column;
    }
    .settings-sidebar {
      flex: 0 0 auto;
      width: auto;
      border-right: none;
    }
    /* A phone's width is too narrow for the pages' usual 2rem sides. */
    .settings-layout .settings-content > :global(.page) {
      padding-left: 1rem;
      padding-right: 1rem;
    }
  }
</style>
