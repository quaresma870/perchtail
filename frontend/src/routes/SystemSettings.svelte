<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { systemSettings } from '../lib/settings'
  import type { SystemSettings } from '../lib/types'

  let loading = true
  let saving = false
  let error = ''

  let searchViewEnabled = true

  onMount(async () => {
    try {
      const settings = await api.get<SystemSettings>('/system-settings')
      searchViewEnabled = settings.search_view_enabled
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load system settings'
    } finally {
      loading = false
    }
  })

  async function toggleSearchView() {
    const next = !searchViewEnabled
    saving = true
    error = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        search_view_enabled: next,
      })
      searchViewEnabled = settings.search_view_enabled
      systemSettings.set(settings)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update system settings'
    } finally {
      saving = false
    }
  }
</script>

<SettingsNav />

<div class="page">
  <h1>System settings</h1>
  <p class="hint">
    Deployment-wide feature toggles — these hide or show a view for every user, separate from what
    a role can access.
  </p>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="card">
      <div class="setting-row">
        <div>
          <div class="setting-name">Search</div>
          <p class="hint">
            Full-text search across indexed sources. Turning this off hides the Search nav entry
            and page for everyone — it doesn't stop or clear background indexing on its own, so
            re-enabling it later picks back up where the index already is.
          </p>
        </div>
        <label class="switch">
          <input
            type="checkbox"
            checked={searchViewEnabled}
            disabled={saving}
            on:change={toggleSearchView}
          />
          <span class="switch-track"></span>
        </label>
      </div>

      <div class="setting-row">
        <div>
          <div class="setting-name">Audit log</div>
          <p class="hint">Coming soon — see ROADMAP.md's "Full audit log viewer" section.</p>
        </div>
        <label class="switch">
          <input type="checkbox" checked={false} disabled />
          <span class="switch-track"></span>
        </label>
      </div>
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 640px;
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .card {
    display: flex;
    flex-direction: column;
  }
  .setting-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1.5rem;
    padding: 1.1rem 1.5rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .setting-row:last-child {
    border-bottom: none;
  }
  .setting-name {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.25rem;
  }
  .setting-row .switch {
    flex: 0 0 auto;
    margin-top: 0.1rem;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
