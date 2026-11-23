<script lang="ts">
  import { onMount } from 'svelte'
  import SettingsLayout from './SettingsLayout.svelte'
  import {
    LOAD_FAILED,
    failureText,
    loadSystemSettings,
    loadSystemWarnings,
  } from '../system-settings-page'
  import type { SystemSettings, SystemWarning } from '../types'

  // One page of system settings: the Settings sidebar, the title, the setup
  // warnings, and the page's cards once /system-settings has loaded and
  // `apply` has taken what the page shows from it.
  export let title: string
  export let apply: (settings: SystemSettings) => void
  // A refused change that belongs to no single row.
  export let error = ''

  let loading = true
  let loadError = ''
  let warnings: SystemWarning[] = []

  onMount(async () => {
    try {
      apply(await loadSystemSettings())
    } catch (err) {
      loadError = failureText(err, LOAD_FAILED)
    } finally {
      loading = false
    }
    warnings = await loadSystemWarnings()
  })
</script>

<SettingsLayout>
  <div class="page settings-blocks">
    <h1>{title}</h1>

    {#if loading}
      <p class="hint">Loading…</p>
    {:else}
      {#if loadError || error}
        <p class="error">{loadError || error}</p>
      {/if}
      {#each warnings as warning (warning.code)}
        <div class="setup-warning card" role="alert" data-testid={`system-warning-${warning.code}`}>
          <strong>{warning.message}</strong>
          <p>{warning.advice}</p>
        </div>
      {/each}

      <div class="grid">
        <slot />
      </div>
    {/if}
  </div>
</SettingsLayout>

<style>
  .page {
    padding: 1.75rem 2rem;
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  /* Settings are grouped in cards that fill the page width (#357): two or
     three columns on a wide screen, one on a narrow one. */
  .grid {
    columns: 3 420px;
    column-gap: 1.1rem;
  }
  .grid > :global(*) {
    break-inside: avoid;
    margin-bottom: 1.1rem;
  }
  .setup-warning {
    border-left: 3px solid var(--warning, #b58105);
    margin-bottom: 1rem;
  }
  .setup-warning p {
    margin: 0.4rem 0 0;
  }
</style>
