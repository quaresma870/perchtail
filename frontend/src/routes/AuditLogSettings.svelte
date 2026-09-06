<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type { AuditLogEntry, AuditLogFilterOptions, AuditLogPage } from '../lib/types'

  const PAGE_SIZE = 50

  let items: AuditLogEntry[] = []
  let total = 0
  let offset = 0
  let loading = true
  let error = ''

  // Built from what's actually in the table (see backend/app/api/audit.py's
  // GET /audit/filters), not a hardcoded action/type list -- new action
  // namespaces show up here automatically as they're written, same
  // "never a guess that goes stale" reasoning as Search's source-name
  // matching (ROADMAP.md).
  let filterOptions: AuditLogFilterOptions = { actions: [], target_types: [] }
  let selectedTargetTypes = new Set<string>()
  let selectedActions = new Set<string>()
  let since = ''
  let until = ''

  function buildQuery(): string {
    const params = new URLSearchParams()
    params.set('limit', String(PAGE_SIZE))
    params.set('offset', String(offset))
    for (const t of selectedTargetTypes) params.append('target_type', t)
    for (const a of selectedActions) params.append('action', a)
    if (since) params.set('since', new Date(since).toISOString())
    if (until) params.set('until', new Date(until).toISOString())
    return params.toString()
  }

  async function load() {
    loading = true
    error = ''
    try {
      const page = await api.get<AuditLogPage>(`/audit?${buildQuery()}`)
      items = page.items
      total = page.total
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load audit log'
    } finally {
      loading = false
    }
  }

  async function loadFilterOptions() {
    try {
      filterOptions = await api.get<AuditLogFilterOptions>('/audit/filters')
    } catch {
      // Non-fatal -- filter checkboxes just start empty; the table itself
      // still loads unfiltered.
    }
  }

  function toggleSetMember(set: Set<string>, value: string): Set<string> {
    const next = new Set(set)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    return next
  }

  function toggleTargetType(value: string) {
    selectedTargetTypes = toggleSetMember(selectedTargetTypes, value)
    offset = 0
    load()
  }

  function toggleAction(value: string) {
    selectedActions = toggleSetMember(selectedActions, value)
    offset = 0
    load()
  }

  function applyDateRange() {
    offset = 0
    load()
  }

  function clearFilters() {
    selectedTargetTypes = new Set()
    selectedActions = new Set()
    since = ''
    until = ''
    offset = 0
    load()
  }

  function nextPage() {
    if (offset + PAGE_SIZE < total) {
      offset += PAGE_SIZE
      load()
    }
  }

  function prevPage() {
    if (offset > 0) {
      offset = Math.max(0, offset - PAGE_SIZE)
      load()
    }
  }

  const formatTimestamp = (iso: string) => new Date(iso).toLocaleString()
  const formatMetadata = (metadata: Record<string, unknown> | null) =>
    metadata ? JSON.stringify(metadata) : ''

  onMount(async () => {
    await Promise.all([loadFilterOptions(), load()])
  })
</script>

<SettingsNav />

<div class="page">
  <h1>Audit log</h1>
  <p class="hint">
    Every login, and every source/rule/role/user/customer/folder/SSO/system-settings change --
    see Settings → System for how long entries are kept before being purged automatically.
  </p>

  <div class="filters card">
    {#if filterOptions.target_types.length > 0}
      <div class="filter-group">
        <div class="filter-label">Type</div>
        <div class="chip-list">
          {#each filterOptions.target_types as targetType (targetType)}
            <label class="chip" class:active={selectedTargetTypes.has(targetType)}>
              <input
                type="checkbox"
                checked={selectedTargetTypes.has(targetType)}
                on:change={() => toggleTargetType(targetType)}
              />
              {targetType}
            </label>
          {/each}
        </div>
      </div>
    {/if}

    {#if filterOptions.actions.length > 0}
      <div class="filter-group">
        <div class="filter-label">Action</div>
        <select
          class="input action-select"
          multiple
          size={Math.min(8, filterOptions.actions.length)}
          on:change={(e) => {
            const picked = Array.from(e.currentTarget.selectedOptions).map((o) => o.value)
            selectedActions = new Set(picked)
            offset = 0
            load()
          }}
        >
          {#each filterOptions.actions as action (action)}
            <option value={action} selected={selectedActions.has(action)}>{action}</option>
          {/each}
        </select>
      </div>
    {/if}

    <div class="filter-group">
      <div class="filter-label">Date range</div>
      <div class="date-range">
        <input type="datetime-local" class="input" bind:value={since} on:change={applyDateRange} />
        <span class="hint">to</span>
        <input type="datetime-local" class="input" bind:value={until} on:change={applyDateRange} />
      </div>
    </div>

    <button type="button" class="btn btn-ghost" on:click={clearFilters}>Clear filters</button>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {:else if loading}
    <p class="hint">Loading…</p>
  {:else if items.length === 0}
    <p class="hint">No matching audit log entries.</p>
  {:else}
    <div class="table-wrap card">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>User</th>
            <th>Action</th>
            <th>Type</th>
            <th>Target</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {#each items as entry (entry.id)}
            <tr>
              <td class="nowrap">{formatTimestamp(entry.timestamp)}</td>
              <td>{entry.username ?? '—'}</td>
              <td><code>{entry.action}</code></td>
              <td>{entry.target_type ?? '—'}</td>
              <td>{entry.target_id ?? '—'}</td>
              <td class="metadata">{formatMetadata(entry.metadata)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="pagination">
      <span class="hint">
        Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
      </span>
      <div class="pagination-buttons">
        <button class="btn btn-ghost" on:click={prevPage} disabled={offset === 0}>Previous</button>
        <button class="btn btn-ghost" on:click={nextPage} disabled={offset + PAGE_SIZE >= total}>
          Next
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 1100px;
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
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 1.5rem;
    padding: 1.1rem 1.5rem;
  }
  .filter-group {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .filter-label {
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }
  .chip-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    max-width: 320px;
  }
  .chip {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.78rem;
    color: var(--text-muted);
    cursor: pointer;
  }
  .chip.active {
    border-color: var(--accent);
    color: var(--text);
    background: var(--accent-soft);
  }
  .chip input {
    margin: 0;
  }
  .action-select {
    min-width: 220px;
  }
  .date-range {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .table-wrap {
    overflow-x: auto;
    padding: 0;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  th,
  td {
    text-align: left;
    padding: 0.55rem 0.9rem;
    border-bottom: 1px solid var(--border-soft);
    color: var(--text);
  }
  th {
    color: var(--text-faint);
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }
  tbody tr:last-child td {
    border-bottom: none;
  }
  .nowrap {
    white-space: nowrap;
  }
  .metadata {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--text-muted);
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  .pagination-buttons {
    display: flex;
    gap: 0.5rem;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
