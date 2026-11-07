<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser, hasCapability } from '../lib/auth'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import SortableHeader from '../lib/components/SortableHeader.svelte'
  import {
    loadViewState,
    matchesText,
    nextSort,
    saveViewState,
    sortRows,
    type SortState,
  } from '../lib/table-view'
  import { sourceStatusView } from '../lib/source-status'
  import { confirmAction } from '../lib/confirm'
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
    agent: 'Agent',
  }

  const customerName = (id: number | null) =>
    id === null ? null : (customers.find((c) => c.id === id)?.name ?? `#${id}`)

  const formatLastSeen = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : null)

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
    const ok = await confirmAction({
      title: 'Delete source',
      message: `Delete the source "${source.name}" and its rules? This can't be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    try {
      await api.delete(`/sources/${source.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete source'
    }
  }

  // Sort and filters (#238), kept for the browser session.
  const VIEW_KEY = 'perchtail.view.sources'
  let view = loadViewState(VIEW_KEY, {
    sort: null as SortState | null,
    text: '',
    protocol: '',
    status: '',
  })
  $: saveViewState(VIEW_KEY, view)

  // Agents first by connection, then enabled, then disabled.
  const statusRank = (s: Source) =>
    !s.enabled ? 3 : s.protocol === 'agent' ? (s.agent_connected ? 0 : 2) : 1
  const placeOf = (s: Source) =>
    [s.customer_name ?? customerName(s.customer_id), s.folder_name].filter(Boolean).join(' / ')
  const accessors: Record<string, (s: Source) => unknown> = {
    name: (s) => s.name,
    customer: (s) => placeOf(s),
    protocol: (s) => PROTOCOL_LABEL[s.protocol],
    status: statusRank,
    rules: (s) => s.rule_count,
  }

  $: filtered = sources.filter(
    (s) =>
      matchesText([s.name, s.host, placeOf(s)], view.text) &&
      (!view.protocol || s.protocol === view.protocol) &&
      (!view.status || (view.status === 'enabled') === s.enabled),
  )
  $: visible = sortRows(filtered, view.sort, accessors)
  $: filtering = Boolean(view.text || view.protocol || view.status)
  $: presentProtocols = [...new Set(sources.map((s) => s.protocol))]
  // A protocol filter kept from earlier in the session can name a protocol
  // no source uses any more: the select would show "All protocols" while
  // the filter still hid every row. Drop it once the list has loaded.
  $: if (!loading && view.protocol && !presentProtocols.includes(view.protocol as Protocol)) {
    view = { ...view, protocol: '' }
  }

  const sortBy = (key: string) => (view = { ...view, sort: nextSort(view.sort, key) })
  const clearFilters = () => (view = { ...view, text: '', protocol: '', status: '' })

  onMount(load)
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Sources</h1>
    {#if hasCapability($currentUser, 'create_source')}
      <button class="btn btn-primary" on:click={() => push('/settings/sources/new')}
        >+ Add source</button
      >
    {/if}
  </div>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
    <div class="filters">
      <input
        class="input filter-text"
        type="search"
        placeholder="Filter by name, host, customer or folder"
        aria-label="Filter sources"
        bind:value={view.text}
      />
      <select class="input" aria-label="Protocol" bind:value={view.protocol}>
        <option value="">All protocols</option>
        {#each presentProtocols as protocol (protocol)}
          <option value={protocol}>{PROTOCOL_LABEL[protocol]}</option>
        {/each}
      </select>
      <select class="input" aria-label="Status" bind:value={view.status}>
        <option value="">Enabled and disabled</option>
        <option value="enabled">Enabled</option>
        <option value="disabled">Disabled</option>
      </select>
      {#if filtering}
        <span class="filter-count">{visible.length} of {sources.length}</span>
        <button class="link" on:click={clearFilters}>Clear filters</button>
      {/if}
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <SortableHeader label="Source" key="name" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Customer" key="customer" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Protocol" key="protocol" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Status" key="status" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Rules" key="rules" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each visible as source (source.id)}
            <tr>
              <td>
                <div class="source-name">
                  <a class="name-link" href={`#/settings/sources/${source.id}`}>{source.name}</a>
                  {#if source.is_system}
                    <span class="badge badge-accent">system</span>
                  {/if}
                  {#if !source.enabled}
                    <span class="badge badge-muted">disabled</span>
                  {/if}
                </div>
                <div class="source-sub">{source.host}</div>
              </td>
              <td class="place">{placeOf(source) || '—'}</td>
              <td>
                <span class="badge protocol-{source.protocol}">{PROTOCOL_LABEL[source.protocol]}</span>
              </td>
              <td>
                {#if source.protocol === 'agent'}
                  {#if source.agent_connected}
                    <span class="status status-ok">✓ connected</span>
                  {:else if formatLastSeen(source.agent_last_seen_at)}
                    <span class="status status-fail" title="Not connected right now"
                      >✕ last seen {formatLastSeen(source.agent_last_seen_at)}</span
                    >
                  {:else}
                    <span class="status status-pending">never connected</span>
                  {/if}
                {:else if checkResults[source.id] === 'checking'}
                  <span class="status status-pending">checking…</span>
                {:else if checkResultOk(source.id) !== null}
                  {#if checkResultOk(source.id)}
                    <span class="status status-ok">✓ reachable</span>
                  {:else}
                    <span class="status status-fail" title={checkResultDetail(source.id)}
                      >✕ failed</span
                    >
                  {/if}
                {:else if source.status === 'ok' || source.status === 'failing'}
                  <!-- The shared, last known status (#276); click to check again. -->
                  <button
                    class="status link"
                    class:status-ok={source.status === 'ok'}
                    class:status-fail={source.status === 'failing'}
                    title={sourceStatusView(source, null, true).tooltip + ' — click to check now'}
                    on:click={() => checkConnection(source)}
                  >
                    {source.status === 'ok' ? '✓ reachable' : '✕ failing'}
                  </button>
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
                  <button
                    class="btn btn-ghost btn-sm"
                    on:click={() => push(`/settings/sources/${source.id}/edit?from=list`)}>Edit</button
                  >
                  <button class="btn btn-danger btn-sm" on:click={() => removeSource(source)}>Delete</button>
                {/if}
              </td>
            </tr>
          {/each}
          {#if sources.length === 0}
            <tr>
              <td colspan="6" class="empty">No sources visible to your account.</td>
            </tr>
          {:else if visible.length === 0}
            <tr>
              <td colspan="6" class="empty">
                No sources match. <button class="link" on:click={clearFilters}>Clear filters</button>
              </td>
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
  table :global(th),
  td {
    text-align: left;
    padding: 0.75rem 1rem;
    font-size: 0.88rem;
  }
  table :global(th) {
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
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.9rem;
  }
  .filter-text {
    flex: 1 1 260px;
    max-width: 420px;
  }
  .filter-count {
    color: var(--text-muted);
    font-size: 0.82rem;
  }
  .place {
    color: var(--text-muted);
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
  .protocol-agent {
    background: var(--protocol-agent-bg);
    color: var(--protocol-agent-text);
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
  .name-link {
    color: inherit;
    text-decoration: none;
  }
  .name-link:hover {
    text-decoration: underline;
  }
</style>
