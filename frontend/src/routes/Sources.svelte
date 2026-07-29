<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser, hasCapability } from '../lib/auth'
  import type { Customer, Protocol, Source } from '../lib/types'

  let sources: Source[] = []
  let customers: Customer[] = []
  let loading = true
  let error = ''
  let checkResults: Record<number, { ok: boolean; detail: string } | 'checking'> = {}

  const PROTOCOL_LABEL: Record<Protocol, string> = {
    ssh: 'SSH',
    smb: 'SMB',
    winrm: 'WinRM',
    local: 'Local',
  }

  const customerName = (id: number | null) =>
    id === null ? null : (customers.find((c) => c.id === id)?.name ?? `#${id}`)

  function checkResultOk(id: number): boolean | null {
    const result = checkResults[id]
    return result && result !== 'checking' ? result.ok : null
  }
  function checkResultDetail(id: number): string {
    const result = checkResults[id]
    return result && result !== 'checking' ? result.detail : ''
  }

  async function load() {
    loading = true
    error = ''
    try {
      sources = await api.get<Source[]>('/sources')
      if (hasCapability($currentUser, 'create_source')) {
        customers = await api.get<Customer[]>('/customers')
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load sources'
    } finally {
      loading = false
    }
  }

  async function checkConnection(source: Source) {
    checkResults = { ...checkResults, [source.id]: 'checking' }
    try {
      const result = await api.post<{ ok: boolean; detail: string }>(
        `/sources/${source.id}/check`,
      )
      checkResults = { ...checkResults, [source.id]: result }
    } catch (err) {
      checkResults = {
        ...checkResults,
        [source.id]: { ok: false, detail: err instanceof ApiError ? err.detail : 'Check failed' },
      }
    }
  }

  async function removeSource(source: Source) {
    if (!confirm(`Delete source "${source.name}"? This cannot be undone.`)) return
    try {
      await api.delete(`/sources/${source.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete source'
    }
  }

  onMount(load)
</script>

<div class="page">
  <div class="header">
    <h1>Sources</h1>
    {#if hasCapability($currentUser, 'create_source')}
      <button class="btn btn-primary" on:click={() => push('/sources/new')}>+ Add source</button>
    {/if}
  </div>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Source</th>
            <th>Protocol</th>
            <th>Status</th>
            <th>Rules</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each sources as source (source.id)}
            <tr>
              <td>
                <div class="source-name">
                  {source.name}
                  {#if source.is_system}
                    <span class="badge badge-accent">system</span>
                  {/if}
                  {#if !source.enabled}
                    <span class="badge badge-muted">disabled</span>
                  {/if}
                </div>
                {#if customerName(source.customer_id)}
                  <div class="source-sub">{customerName(source.customer_id)}</div>
                {/if}
              </td>
              <td>
                <span class="badge protocol-{source.protocol}">{PROTOCOL_LABEL[source.protocol]}</span>
              </td>
              <td>
                {#if checkResults[source.id] === 'checking'}
                  <span class="status status-pending">checking…</span>
                {:else if checkResultOk(source.id) !== null}
                  {#if checkResultOk(source.id)}
                    <span class="status status-ok">✓ reachable</span>
                  {:else}
                    <span class="status status-fail" title={checkResultDetail(source.id)}
                      >✕ failed</span
                    >
                  {/if}
                {:else}
                  <button class="status status-pending link" on:click={() => checkConnection(source)}>
                    ⏱ check
                  </button>
                {/if}
              </td>
              <td class="rules-count">{source.rule_count} rule{source.rule_count === 1 ? '' : 's'}</td>
              <td class="actions">
                <button
                  class="icon-btn"
                  title="Browse"
                  on:click={() => push(`/viewer/${source.id}`)}
                >
                  ▶
                </button>
                {#if !source.is_system && hasCapability($currentUser, 'create_source')}
                  <button class="link" on:click={() => push(`/sources/${source.id}`)}>edit</button>
                  <button class="link danger" on:click={() => removeSource(source)}>delete</button>
                {/if}
              </td>
            </tr>
          {/each}
          {#if sources.length === 0}
            <tr>
              <td colspan="5" class="empty">No sources visible to your account.</td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 0.75rem 1rem;
    font-size: 0.88rem;
  }
  th {
    color: var(--text-faint);
    font-weight: 600;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border);
  }
  tbody tr {
    border-bottom: 1px solid var(--border-soft);
  }
  tbody tr:last-child {
    border-bottom: none;
  }
  tbody tr:hover {
    background: var(--bg-hover);
  }
  .source-name {
    font-weight: 600;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .source-sub {
    color: var(--text-faint);
    font-size: 0.8rem;
    margin-top: 0.15rem;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .badge-muted {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .protocol-ssh {
    background: var(--protocol-ssh-bg);
    color: var(--protocol-ssh-text);
  }
  .protocol-smb {
    background: var(--protocol-smb-bg);
    color: var(--protocol-smb-text);
  }
  .protocol-winrm {
    background: var(--protocol-winrm-bg);
    color: var(--protocol-winrm-text);
  }
  .protocol-local {
    background: var(--protocol-local-bg);
    color: var(--protocol-local-text);
  }
  .status {
    font-size: 0.82rem;
    font-weight: 600;
  }
  .status-ok {
    color: var(--success);
  }
  .status-fail {
    color: var(--danger);
    cursor: help;
  }
  .status-pending {
    color: var(--text-faint);
  }
  button.status.link {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
    font: inherit;
  }
  .rules-count {
    color: var(--text-muted);
  }
  .actions {
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .icon-btn {
    border: 1px solid var(--border);
    background: var(--bg-elevated-2);
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    width: 1.8rem;
    height: 1.8rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 0.7rem;
  }
  .icon-btn:hover {
    color: var(--accent-hover);
    border-color: var(--accent-border);
  }
  button.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    padding: 0;
    font-size: 0.85rem;
  }
  button.link.danger {
    color: var(--danger);
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 2rem;
  }
  .error {
    color: var(--danger);
  }
  .hint {
    color: var(--text-faint);
  }
</style>
