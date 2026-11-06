<script lang="ts">
  // Settings -> System -> Updates (#215), for super admins: the running
  // version, the latest release, and how to upgrade the way this instance
  // was installed. No one-click upgrade: that would need the Docker socket
  // or write access to the install, which PerchTail doesn't get.
  import { onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import { systemSettings } from '../settings'
  import type { SystemSettings, UpdateStatus } from '../types'
  import { upgradeSteps } from '../upgrade-steps'

  let status: UpdateStatus | null = null
  let error = ''
  let checking = false
  let toggling = false

  const formatDate = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : null)

  async function load() {
    try {
      status = await api.get<UpdateStatus>('/system/update')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load the update status'
    }
  }

  async function checkNow() {
    checking = true
    error = ''
    try {
      status = await api.post<UpdateStatus>('/system/update/check')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to check for updates'
    } finally {
      checking = false
    }
  }

  async function toggle() {
    if (!status) return
    toggling = true
    error = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        update_check_enabled: !status.enabled,
      })
      systemSettings.set(settings)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to change the release check'
    } finally {
      toggling = false
    }
  }

  onMount(load)

  $: steps = status?.latest_version ? upgradeSteps(status.install_method, status.latest_version) : ''
</script>

<div class="card updates">
  <div class="setting-row">
    <div>
      <div class="setting-name">Updates</div>
      <p class="hint">
        Once a day, PerchTail asks GitHub which release is the latest. Nothing about this
        installation is sent. Turn it off here, or with <code>UPDATE_CHECK=false</code> for
        installations without internet access.
      </p>
    </div>
    {#if status}
      <label class="switch" title={status.disabled_by_env ? 'Turned off by UPDATE_CHECK=false' : ''}>
        <input
          type="checkbox"
          aria-label="Check for new releases"
          checked={status.enabled}
          disabled={toggling || status.disabled_by_env}
          on:change={toggle}
        />
        <span class="switch-track"></span>
      </label>
    {/if}
  </div>

  <div class="setting-row column">
  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if status}
    <dl class="facts">
      <dt>Running</dt>
      <dd>
        <strong>{status.running_version}</strong>
        ({status.install_method === 'docker' ? 'Docker image' : 'installed from source'})
      </dd>
      <dt>Latest release</dt>
      <dd>
        {#if status.latest_version}
          {status.latest_version}
          {#if status.latest_published_at}· {formatDate(status.latest_published_at)}{/if}
          {#if status.latest_url}
            · <a href={status.latest_url} target="_blank" rel="noopener noreferrer">release notes</a>
          {/if}
        {:else}
          not known yet
        {/if}
      </dd>
      <dt>Last check</dt>
      <dd>
        {#if status.disabled_by_env}
          off (<code>UPDATE_CHECK=false</code>)
        {:else if !status.enabled}
          off
        {:else if status.last_error}
          <span class="warn">couldn't check: {status.last_error}</span>
          {#if status.last_success_at}(last success {formatDate(status.last_success_at)}){/if}
        {:else if status.last_checked_at}
          {formatDate(status.last_checked_at)}
        {:else}
          not yet
        {/if}
      </dd>
    </dl>

    {#if status.update_available}
      <div class="notice" role="status">
        <strong>Version {status.latest_version} is available.</strong> Read its release notes first:
        they say when an upgrade needs more than this (a migration, upgrading the push agents).
        <pre class="steps">{steps}</pre>
      </div>
    {:else if status.latest_version}
      <p class="hint ok">✓ This is the latest release.</p>
    {/if}

    <button
      type="button"
      class="btn btn-ghost"
      on:click={checkNow}
      disabled={checking || !status.enabled}
    >
      {checking ? 'Checking…' : 'Check now'}
    </button>
  {/if}
  </div>
</div>

<style>
  /* The setting-row look of routes/SystemSettings.svelte, whose styles are
     scoped to that page. */
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
  .setting-row.column {
    flex-direction: column;
    gap: 0.75rem;
  }
  .setting-row .switch {
    flex: 0 0 auto;
    margin-top: 0.1rem;
  }
  .setting-name {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.25rem;
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
  .facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.35rem 1rem;
    margin: 0;
    font-size: 0.88rem;
  }
  .facts dt {
    color: var(--text-faint);
  }
  .facts dd {
    margin: 0;
    color: var(--text);
  }
  .warn {
    color: var(--warning);
  }
  .ok {
    color: var(--success, var(--text-muted));
  }
  .notice {
    padding: 0.75rem 1rem;
    border-radius: 8px;
    background: var(--warning-soft);
    font-size: 0.86rem;
    line-height: 1.5;
  }
  .steps {
    margin: 0.6rem 0 0;
    padding: 0.6rem 0.8rem;
    border-radius: 6px;
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: 0.78rem;
    white-space: pre-wrap;
    overflow-x: auto;
  }
</style>
