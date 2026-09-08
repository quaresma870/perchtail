<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import type { Alert, AlertCreate, Source } from '../lib/types'

  let alerts: Alert[] = []
  let sources: Source[] = []
  let loading = true
  let error = ''

  let showCreate = false
  let creating = false
  let createError = ''
  let name = ''
  let query = ''
  let sourceId: number | '' = ''
  let webhookUrl = ''

  let testResult: Record<number, 'sending' | boolean> = {}

  const sourceName = (id: number | null) =>
    id === null ? 'All sources you can view' : (sources.find((s) => s.id === id)?.name ?? `#${id}`)

  const formatLastChecked = (iso: string | null) =>
    iso ? new Date(iso).toLocaleString() : 'never'

  async function load() {
    loading = true
    error = ''
    try {
      ;[alerts, sources] = await Promise.all([
        api.get<Alert[]>('/alerts'),
        api.get<Source[]>('/sources'),
      ])
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load alerts'
    } finally {
      loading = false
    }
  }

  async function createAlert() {
    createError = ''
    creating = true
    try {
      const payload: AlertCreate = {
        name,
        query,
        webhook_url: webhookUrl,
        source_id: sourceId === '' ? null : sourceId,
      }
      await api.post<Alert>('/alerts', payload)
      name = ''
      query = ''
      sourceId = ''
      webhookUrl = ''
      showCreate = false
      await load()
    } catch (err) {
      createError = err instanceof ApiError ? err.detail : 'Failed to create alert'
    } finally {
      creating = false
    }
  }

  async function toggleEnabled(alert: Alert) {
    try {
      await api.patch<Alert>(`/alerts/${alert.id}`, { enabled: !alert.enabled })
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update alert'
    }
  }

  async function removeAlert(alert: Alert) {
    if (!confirm(`Delete alert "${alert.name}"? This cannot be undone.`)) return
    try {
      await api.delete(`/alerts/${alert.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete alert'
    }
  }

  async function sendTest(alert: Alert) {
    testResult = { ...testResult, [alert.id]: 'sending' }
    try {
      const result = await api.post<{ ok: boolean }>(`/alerts/${alert.id}/test`)
      testResult = { ...testResult, [alert.id]: result.ok }
    } catch {
      testResult = { ...testResult, [alert.id]: false }
    }
  }

  onMount(load)
</script>

<div class="page">
  <div class="header">
    <div>
      <h1>Alerts</h1>
      <p class="hint">
        Get a webhook notification when new content matching a saved query shows up in a source's
        search index. Only sources with full-text search enabled can be watched — see
        <a href="#/search">Search</a>.
      </p>
    </div>
    <button class="btn btn-primary" on:click={() => (showCreate = !showCreate)}>
      {showCreate ? 'Cancel' : '+ New alert'}
    </button>
  </div>

  {#if showCreate}
    <form class="card create-form" on:submit|preventDefault={createAlert}>
      {#if createError}
        <p class="error">{createError}</p>
      {/if}
      <label>
        Name
        <input class="input" bind:value={name} required />
      </label>
      <label>
        Query
        <input class="input" bind:value={query} placeholder="e.g. connection refused" required />
      </label>
      <label>
        Source
        <select class="input" bind:value={sourceId}>
          <option value="">All sources you can view</option>
          {#each sources as source (source.id)}
            <option value={source.id}>{source.name}</option>
          {/each}
        </select>
      </label>
      <label>
        Webhook URL
        <input
          class="input"
          type="url"
          bind:value={webhookUrl}
          placeholder="https://example.com/hook"
          required
        />
      </label>
      <button class="btn btn-primary" type="submit" disabled={creating}>
        {creating ? 'Creating…' : 'Create alert'}
      </button>
    </form>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {:else if loading}
    <p class="hint">Loading…</p>
  {:else if alerts.length === 0}
    <p class="hint">No alerts yet.</p>
  {:else}
    <ul class="alert-list">
      {#each alerts as alert (alert.id)}
        <li class="card alert-row">
          <div class="alert-main">
            <div class="alert-name">{alert.name}</div>
            <div class="alert-meta">
              <span class="badge badge-accent">{sourceName(alert.source_id)}</span>
              <code class="query">{alert.query}</code>
            </div>
            <div class="alert-sub">
              {alert.webhook_url} · last checked {formatLastChecked(alert.last_checked_at)}
            </div>
          </div>
          <div class="alert-actions">
            {#if testResult[alert.id] === 'sending'}
              <span class="hint">Sending…</span>
            {:else if testResult[alert.id] === true}
              <span class="test-ok">Sent ✓</span>
            {:else if testResult[alert.id] === false}
              <span class="error">Failed</span>
            {/if}
            <button class="btn btn-ghost" on:click={() => sendTest(alert)}>Test</button>
            <label class="switch">
              <input
                type="checkbox"
                checked={alert.enabled}
                on:change={() => toggleEnabled(alert)}
              />
              <span class="switch-track"></span>
            </label>
            <button class="btn btn-ghost danger" on:click={() => removeAlert(alert)}>Delete</button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 820px;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  .header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1.5rem;
  }
  .header button {
    white-space: nowrap;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0.35rem 0 0;
    line-height: 1.5;
  }
  .create-form {
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
    padding: 1.1rem 1.5rem;
  }
  .create-form label {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .create-form button {
    align-self: flex-start;
  }
  .alert-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .alert-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1.5rem;
    padding: 0.9rem 1.2rem;
  }
  .alert-name {
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.3rem;
  }
  .alert-meta {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8rem;
    margin-bottom: 0.25rem;
  }
  .alert-meta .query {
    font-family: var(--font-mono);
    color: var(--text-muted);
  }
  .alert-sub {
    font-size: 0.75rem;
    color: var(--text-faint);
  }
  .alert-actions {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    flex: 0 0 auto;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .btn-ghost.danger {
    color: var(--danger);
  }
  .test-ok {
    color: var(--success);
    font-size: 0.8rem;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
