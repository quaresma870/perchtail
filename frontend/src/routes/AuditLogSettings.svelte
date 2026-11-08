<script lang="ts">
  import FilterDropdown from '../lib/components/FilterDropdown.svelte'
  import SortableHeader from '../lib/components/SortableHeader.svelte'
  import { nextSort, type SortState } from '../lib/table-view'
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import { detailPairs, targetHref, targetTypeLabel } from '../lib/audit-format'
  import { currentUser } from '../lib/auth'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { confirmAction } from '../lib/confirm'
  import type {
    AuditIntegrityStatus,
    AuditLogEntry,
    AuditLogFilterOptions,
    AuditLogPage,
  } from '../lib/types'

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
  let filterOptions: AuditLogFilterOptions = { actions: [], target_types: [], targets: {} }
  let selectedTargetTypes = new Set<string>()
  let selectedActions = new Set<string>()
  // Click a column header to sort by it (#358); the server sorts, so every
  // matching entry is in order, not just this page. Newest first by default.
  let sort: SortState = { key: 'time', dir: 'desc' }

  function sortBy(key: string) {
    sort = nextSort(sort, key)
    offset = 0
    load()
  }
  let since = ''
  let until = ''

  let integrity: AuditIntegrityStatus = {
    status: 'unknown',
    last_checked_at: null,
    broken_row_id: null,
    race_break_count: 0,
    known_break_count: 0,
    known_breaks_recorded_at: null,
  }
  let verifying = false
  let recordingKnownBreaks = false

  $: isSuperAdmin = $currentUser?.is_super_admin ?? false
  $: raceBreakText = `${integrity.race_break_count} ${integrity.race_break_count === 1 ? 'break matches' : 'breaks match'}`

  function buildQuery(): string {
    const params = new URLSearchParams()
    params.set('limit', String(PAGE_SIZE))
    params.set('offset', String(offset))
    params.set('sort', sort.key)
    params.set('order', sort.dir)
    for (const t of selectedTargetTypes) params.append('target_type', t)
    for (const t of selectedTargets) params.append('target', t)
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

  async function loadIntegrityStatus() {
    try {
      integrity = await api.get<AuditIntegrityStatus>('/audit/integrity')
    } catch {
      // Non-fatal -- the banner just doesn't render; the table itself still
      // loads unaffected.
    }
  }

  async function verifyIntegrityNow() {
    verifying = true
    try {
      integrity = await api.post<AuditIntegrityStatus>('/audit/integrity/verify')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to verify audit log integrity'
    } finally {
      verifying = false
    }
  }

  async function recordKnownBreaks() {
    const confirmed = await confirmAction({
      title: 'Record known breaks',
      message:
        `Record the ${integrity.race_break_count} ${integrity.race_break_count === 1 ? 'break' : 'breaks'} ` +
        'from the #213 race as known? Verification will then accept ' +
        'exactly these rows. Any other break is still reported as tampering. This is ' +
        'itself written to the audit log.',
      confirmLabel: 'Record as known',
    })
    if (!confirmed) return
    recordingKnownBreaks = true
    try {
      integrity = await api.post<AuditIntegrityStatus>('/audit/integrity/known-breaks')
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to record the known breaks'
    } finally {
      recordingKnownBreaks = false
    }
  }

  // One dropdown per target type (#358): "Every <type>" filters on the
  // whole type, the other options on single targets ("source:5").
  let selectedTargets = new Set<string>()
  const EVERY = '*'

  function typeOptions(kind: string, options: AuditLogFilterOptions) {
    return [
      { value: EVERY, label: `Every ${targetTypeLabel(kind).toLowerCase()}` },
      ...(options.targets[kind] ?? []).map((t) => ({ value: String(t.id), label: t.name })),
    ]
  }

  function typeSelection(kind: string, types: Set<string>, targets: Set<string>): Set<string> {
    const picked = new Set<string>()
    if (types.has(kind)) picked.add(EVERY)
    for (const t of targets) {
      const [k, id] = t.split(':')
      if (k === kind) picked.add(id)
    }
    return picked
  }

  function setTypeSelection(kind: string, picked: Set<string>) {
    const types = new Set(selectedTargetTypes)
    if (picked.has(EVERY)) types.add(kind)
    else types.delete(kind)
    const targets = new Set([...selectedTargets].filter((t) => !t.startsWith(`${kind}:`)))
    for (const id of picked) if (id !== EVERY) targets.add(`${kind}:${id}`)
    selectedTargetTypes = types
    selectedTargets = targets
    offset = 0
    load()
  }

  function setActions(picked: Set<string>) {
    selectedActions = picked
    offset = 0
    load()
  }

  function applyDateRange() {
    offset = 0
    load()
  }

  function clearFilters() {
    selectedTargetTypes = new Set()
    selectedTargets = new Set()
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

  onMount(async () => {
    await Promise.all([loadFilterOptions(), loadIntegrityStatus(), load()])
  })
</script>

<SettingsNav />

<div class="page">
  <h1>Audit log</h1>
  <p class="hint">
    Every login, and every source/rule/role/user/customer/folder/SSO/system-settings change --
    see Settings → System for how long entries are kept before being purged automatically.
  </p>

  <div
    class="integrity-banner"
    class:broken={integrity.status === 'broken'}
    class:race={integrity.status === 'race_breaks'}
    class:unknown={integrity.status === 'unknown'}
  >
    {#if integrity.status === 'broken'}
      <span class="integrity-text">
        ⚠ Tampering detected: the audit log's hash chain breaks at row {integrity.broken_row_id}.
        Everything from that row onward can no longer be trusted as an unaltered record.
        {#if integrity.race_break_count > 0}
          Separately, {raceBreakText} the #213 race (concurrent writes before 0.5.2).
        {/if}
      </span>
    {:else if integrity.status === 'race_breaks'}
      <span class="integrity-text">
        ⚠ {raceBreakText} the #213 race, starting at row {integrity.broken_row_id}: concurrent
        audit writes before 0.5.2 forked the chain. These rows were written by PerchTail itself, so
        this isn't tampering, but verification can't accept them until a super admin records them
        as known.
      </span>
    {:else if integrity.status === 'ok'}
      <span class="integrity-text">
        ✓ Audit log integrity verified -- chain intact as of
        {integrity.last_checked_at ? formatTimestamp(integrity.last_checked_at) : 'unknown'}.
        {#if integrity.known_break_count > 0}
          {integrity.known_break_count} known
          {integrity.known_break_count === 1 ? 'break' : 'breaks'} from the #213 race recorded{integrity.known_breaks_recorded_at
            ? ` on ${formatTimestamp(integrity.known_breaks_recorded_at)}`
            : ''}.
        {/if}
      </span>
    {:else}
      <span class="integrity-text">
        Audit log integrity has not been checked yet -- it runs automatically on its own
        schedule (Settings → System), or verify it now.
      </span>
    {/if}
    <div class="integrity-actions">
      {#if isSuperAdmin && integrity.race_break_count > 0}
        <button
          type="button"
          class="btn btn-ghost"
          on:click={recordKnownBreaks}
          disabled={recordingKnownBreaks || verifying}
        >
          {recordingKnownBreaks ? 'Recording…' : 'Mark as known'}
        </button>
      {/if}
      <button type="button" class="btn btn-ghost" on:click={verifyIntegrityNow} disabled={verifying}>
        {verifying ? 'Verifying…' : 'Verify now'}
      </button>
    </div>
  </div>

  <div class="filters card">
    {#if filterOptions.actions.length > 0}
      <div class="filter-group">
        <div class="filter-label">Action</div>
        <FilterDropdown
          label="Actions"
          noneLabel="All actions"
          options={filterOptions.actions.map((a) => ({ value: a, label: a, mono: true }))}
          selected={selectedActions}
          on:change={(e) => setActions(e.detail)}
        />
      </div>
    {/if}

    {#each filterOptions.target_types as kind (kind)}
      <div class="filter-group">
        <div class="filter-label">{targetTypeLabel(kind)}</div>
        <FilterDropdown
          label={targetTypeLabel(kind)}
          noneLabel="Any"
          options={typeOptions(kind, filterOptions)}
          selected={typeSelection(kind, selectedTargetTypes, selectedTargets)}
          on:change={(e) => setTypeSelection(kind, e.detail)}
        />
      </div>
    {/each}

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
            <SortableHeader label="Time" key="time" {sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="User" key="user" {sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Action" key="action" {sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Target" key="target" {sort} on:sort={(e) => sortBy(e.detail)} />
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {#each items as entry (entry.id)}
            {@const href = targetHref(entry)}
            <tr>
              <td class="nowrap">{formatTimestamp(entry.timestamp)}</td>
              <td>{entry.username ?? '—'}</td>
              <td><code>{entry.action}</code></td>
              <td
                class="target"
                title={entry.target_type
                  ? `${entry.target_type}${entry.target_id !== null ? ` #${entry.target_id}` : ''}`
                  : ''}
              >
                {#if entry.target_type}
                  <span class="target-type">{targetTypeLabel(entry.target_type)}</span>
                  {#if entry.target_name}
                    {#if href}<a {href}>{entry.target_name}</a>{:else}<span>{entry.target_name}</span>{/if}
                    {#if entry.target_context}
                      <span class="target-context">{entry.target_context}</span>
                    {/if}
                    {#if entry.target_name_is_current}
                      <span
                        class="target-note"
                        title="This entry predates 0.5.3 and recorded only an id; this is the object's name now."
                        >current name</span
                      >
                    {/if}
                  {:else if entry.target_id !== null}
                    <span class="target-context">#{entry.target_id}, no longer exists</span>
                  {/if}
                {:else}
                  —
                {/if}
              </td>
              <td class="details">
                {#each detailPairs(entry.metadata) as [key, value] (key)}
                  <span class="detail"><span class="detail-key">{key}</span> {value}</span>
                {/each}
              </td>
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
  /* The page and its table use the whole width (#358). */
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
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .integrity-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.7rem 1rem;
    border-radius: 8px;
    background: var(--success-soft);
    font-size: 0.85rem;
  }
  .integrity-banner.broken {
    background: var(--danger-soft);
  }
  .integrity-banner.race {
    background: var(--warning-soft);
  }
  .integrity-banner.unknown {
    background: var(--border-soft);
  }
  .integrity-actions {
    display: flex;
    gap: 0.5rem;
    flex-shrink: 0;
  }
  .integrity-text {
    color: var(--text);
    line-height: 1.4;
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
  .target {
    width: 22%;
  }
  .target-type {
    display: block;
    font-size: 0.7rem;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .target-context {
    display: block;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .target-note {
    font-size: 0.7rem;
    color: var(--text-faint);
    font-style: italic;
  }
  /* max-width 0 with a width lets the cell take the rest of the row and
     still cut long details with an ellipsis. */
  .details {
    width: 45%;
    max-width: 0;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .detail {
    display: block;
    font-family: var(--font-mono);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .detail-key {
    color: var(--text-faint);
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
