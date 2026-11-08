<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser, hasCapability } from '../lib/auth'
  import Modal from '../lib/components/Modal.svelte'
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
  import {
    credentialAgeDays,
    credentialAgeLabel,
    credentialSortKey,
    isCredentialStale,
    parseUtc,
  } from '../lib/credential-age'
  import { systemSettings } from '../lib/settings'
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

  // quiet: refresh the rows without swapping the table for "Loading…".
  async function load(quiet = false) {
    if (!quiet) loading = true
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
      await load(true)
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
    credential: '',
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
    credential: (s) => credentialSortKey(s),
  }

  // Credential age (#248): 0 in Settings -> System turns the warning off.
  $: maxCredentialAge = $systemSettings.credential_max_age_days
  function credentialTitle(source: Source, maxAge: number): string {
    if (!source.has_credential) return ''
    if (!source.credential_updated_at) {
      return "Set before PerchTail kept the date: unknown until it's next changed"
    }
    const when = `Set on ${parseUtc(source.credential_updated_at).toLocaleDateString()}`
    return isCredentialStale(source, maxAge)
      ? `${when}, ${credentialAgeDays(source)} days ago: older than the ${maxAge}-day limit in Settings → System`
      : when
  }

  $: filtered = sources.filter(
    (s) =>
      matchesText([s.name, s.host, placeOf(s)], view.text) &&
      (!view.protocol || s.protocol === view.protocol) &&
      (!view.status || (view.status === 'enabled') === s.enabled) &&
      (!view.credential || isCredentialStale(s, maxCredentialAge)),
  )
  $: visible = sortRows(filtered, view.sort, accessors)
  $: filtering = Boolean(view.text || view.protocol || view.status || view.credential)
  $: presentProtocols = [...new Set(sources.map((s) => s.protocol))]
  // A protocol filter kept from earlier in the session can name a protocol
  // no source uses any more: the select would show "All protocols" while
  // the filter still hid every row. Drop it once the list has loaded.
  $: if (!loading && view.protocol && !presentProtocols.includes(view.protocol as Protocol)) {
    view = { ...view, protocol: '' }
  }
  // Likewise when the warning was turned off since.
  $: if (view.credential && maxCredentialAge <= 0) view = { ...view, credential: '' }

  const sortBy = (key: string) => (view = { ...view, sort: nextSort(view.sort, key) })
  const clearFilters = () =>
    (view = { ...view, text: '', protocol: '', status: '', credential: '' })

  // Bulk credential update (#248), for whoever may edit sources. Only the
  // visible rows count, so a filter never hides what is about to change.
  const MAX_BULK = 200
  $: canManage = hasCapability($currentUser, 'create_source')
  const usesCredential = (s: Source) =>
    !s.is_system && (s.protocol === 'ssh' || s.protocol === 'smb' || s.protocol === 'winrm')
  let selected = new Set<number>()
  $: selectable = visible.filter(usesCredential)
  $: selectedSources = selectable.filter((s) => selected.has(s.id))
  $: allSelected = selectable.length > 0 && selectedSources.length === selectable.length

  function toggleRow(id: number, on: boolean) {
    const next = new Set(selected)
    if (on) next.add(id)
    else next.delete(id)
    selected = next
  }
  function toggleAll(on: boolean) {
    const next = new Set(selected)
    for (const s of selectable) {
      if (on) next.add(s.id)
      else next.delete(s.id)
    }
    selected = next
  }

  interface BulkResult {
    source_id: number
    name: string | null
    status: 'updated' | 'failed' | 'skipped'
    detail: string | null
  }

  let bulkOpen = false
  let bulkTargets: Source[] = []
  let bulkUsername = ''
  let bulkMode: 'password' | 'key' = 'password'
  let bulkPassword = ''
  let bulkKey = ''
  let bulkTestFirst = true
  let bulkRunning = false
  let bulkError = ''
  let bulkResults: BulkResult[] | null = null

  // A private key only signs in over SSH.
  $: bulkAllSsh = bulkTargets.every((s) => s.protocol === 'ssh')
  $: if (!bulkAllSsh && bulkMode === 'key') bulkMode = 'password'
  $: bulkSecret = bulkMode === 'key' ? bulkKey : bulkPassword
  $: bulkCount = (status: BulkResult['status']) =>
    (bulkResults ?? []).filter((r) => r.status === status).length
  $: bulkProblems = (bulkResults ?? []).filter((r) => r.status !== 'updated')

  function openBulk() {
    bulkTargets = selectedSources
    bulkUsername = ''
    bulkMode = 'password'
    bulkPassword = ''
    bulkKey = ''
    bulkTestFirst = true
    bulkError = ''
    bulkResults = null
    bulkOpen = true
  }

  function closeBulk() {
    // The request carries on either way; keep the popup until it answers.
    if (bulkRunning) return
    bulkPassword = ''
    bulkKey = ''
    bulkOpen = false
  }

  async function runBulk() {
    bulkError = ''
    bulkRunning = true
    try {
      const credential =
        bulkMode === 'key'
          ? { username: bulkUsername, private_key: bulkKey }
          : { username: bulkUsername, password: bulkPassword }
      bulkResults = await api.post<BulkResult[]>('/sources/credentials', {
        source_ids: bulkTargets.map((s) => s.id),
        credential,
        test_first: bulkTestFirst,
      })
      bulkPassword = ''
      bulkKey = ''
      const updated = new Set(bulkResults.filter((r) => r.status === 'updated').map((r) => r.source_id))
      selected = new Set([...selected].filter((id) => !updated.has(id)))
      await load(true)
    } catch (err) {
      bulkError = err instanceof ApiError ? err.detail : 'Failed to update the credentials'
    } finally {
      bulkRunning = false
    }
  }

  onMount(() => load())
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Sources</h1>
    {#if canManage}
      <div class="header-actions">
        <button
          class="btn btn-ghost"
          disabled={selectedSources.length === 0}
          title={selectedSources.length === 0
            ? 'Select SSH, SMB or WinRM sources in the list first'
            : 'Set one new credential on the selected sources'}
          on:click={openBulk}>Update credentials ({selectedSources.length})</button
        >
        <button class="btn btn-primary" on:click={() => push('/settings/sources/new')}
          >+ Add source</button
        >
      </div>
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
      {#if maxCredentialAge > 0}
        <select class="input" aria-label="Credential age" bind:value={view.credential}>
          <option value="">Any credential age</option>
          <option value="stale">Credentials older than {maxCredentialAge} days</option>
        </select>
      {/if}
      {#if filtering}
        <span class="filter-count">{visible.length} of {sources.length}</span>
        <button class="link" on:click={clearFilters}>Clear filters</button>
      {/if}
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            {#if canManage}
              <th class="select-cell">
                <input
                  type="checkbox"
                  aria-label="Select all visible sources"
                  checked={allSelected}
                  disabled={selectable.length === 0}
                  on:change={(e) => toggleAll(e.currentTarget.checked)}
                />
              </th>
            {/if}
            <SortableHeader label="Source" key="name" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Customer" key="customer" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Protocol" key="protocol" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Status" key="status" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Credential" key="credential" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <SortableHeader label="Rules" key="rules" sort={view.sort} on:sort={(e) => sortBy(e.detail)} />
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each visible as source (source.id)}
            <tr>
              {#if canManage}
                <td class="select-cell">
                  {#if usesCredential(source)}
                    <input
                      type="checkbox"
                      aria-label={`Select ${source.name}`}
                      checked={selected.has(source.id)}
                      on:change={(e) => toggleRow(source.id, e.currentTarget.checked)}
                    />
                  {/if}
                </td>
              {/if}
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
                {#if !source.enabled}
                  <span class="status status-pending" title="Turned off: nothing is read from it">disabled</span>
                {:else if source.protocol === 'agent'}
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
              <td class="credential-age">
                {#if isCredentialStale(source, maxCredentialAge)}
                  <span class="credential-stale" title={credentialTitle(source, maxCredentialAge)}
                    >⚠ {credentialAgeLabel(source)}</span
                  >
                {:else}
                  <span title={credentialTitle(source, maxCredentialAge) || undefined}>{credentialAgeLabel(source)}</span>
                {/if}
              </td>
              <td class="rules-count">{source.rule_count} rule{source.rule_count === 1 ? '' : 's'}</td>
              <td class="actions">
                <button
                  class="icon-btn"
                  title={source.enabled ? 'Browse' : 'Disabled: turn it on in its settings to browse it'}
                  disabled={!source.enabled}
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
              <td colspan={canManage ? 8 : 7} class="empty">No sources visible to your account.</td>
            </tr>
          {:else if visible.length === 0}
            <tr>
              <td colspan={canManage ? 8 : 7} class="empty">
                No sources match. <button class="link" on:click={clearFilters}>Clear filters</button>
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if bulkOpen}
  <Modal title="Update credentials" on:close={closeBulk}>
    {#if bulkResults}
      <p class="bulk-summary" role="status">
        {bulkCount('updated')} updated, {bulkCount('failed')} failed, {bulkCount('skipped')} skipped
      </p>
      {#if bulkProblems.length > 0}
        <ul class="bulk-problems">
          {#each bulkProblems as result (result.source_id)}
            <li>
              <span class="bulk-status bulk-{result.status}">{result.status}</span>
              <strong>{result.name ?? `#${result.source_id}`}</strong>{result.detail
                ? `: ${result.detail}`
                : ''}
            </li>
          {/each}
        </ul>
      {/if}
    {:else}
      <p class="hint">
        Sets the same credential on the {bulkTargets.length} selected source{bulkTargets.length === 1
          ? ''
          : 's'}. Each one keeps its own encrypted copy.
      </p>
      <label class="field">
        Username
        <input class="input" bind:value={bulkUsername} autocomplete="off" disabled={bulkRunning} />
      </label>
      {#if bulkAllSsh}
        <div class="auth-choice" role="radiogroup" aria-label="Sign in with">
          <label
            ><input type="radio" bind:group={bulkMode} value="password" disabled={bulkRunning} /> Password</label
          >
          <label
            ><input type="radio" bind:group={bulkMode} value="key" disabled={bulkRunning} /> Private key</label
          >
        </div>
      {/if}
      {#if bulkMode === 'key'}
        <label class="field">
          New private key: ed25519, ECDSA or RSA, without a passphrase
          <textarea
            class="input mono"
            rows="4"
            wrap="off"
            bind:value={bulkKey}
            disabled={bulkRunning}
            spellcheck="false"
            autocomplete="off"
            autocapitalize="off"
            data-gramm="false"
            data-gramm_editor="false"
            data-enable-grammarly="false"
          ></textarea>
        </label>
      {:else}
        <label class="field">
          New password
          <input
            class="input"
            type="password"
            bind:value={bulkPassword}
            disabled={bulkRunning}
            autocomplete="new-password"
            spellcheck="false"
            data-gramm="false"
            data-enable-grammarly="false"
          />
        </label>
      {/if}
      <label class="check-field">
        <input type="checkbox" bind:checked={bulkTestFirst} disabled={bulkRunning} />
        Test the new credential on each source first and update only where it works
      </label>
      {#if bulkTargets.length > MAX_BULK}
        <p class="error">Select at most {MAX_BULK} sources at a time.</p>
      {/if}
      {#if bulkRunning}
        <p class="hint" role="status">
          Updating {bulkTargets.length} source{bulkTargets.length === 1 ? '' : 's'}…
        </p>
      {/if}
      {#if bulkError}
        <p class="error" role="alert">{bulkError}</p>
      {/if}
    {/if}
    <svelte:fragment slot="footer">
      {#if bulkResults}
        <button class="btn btn-primary" type="button" on:click={closeBulk}>Done</button>
      {:else}
        <button class="btn btn-ghost" type="button" disabled={bulkRunning} on:click={closeBulk}
          >Cancel</button
        >
        <button
          class="btn btn-primary"
          type="button"
          disabled={bulkRunning ||
            !bulkUsername ||
            !bulkSecret ||
            bulkTargets.length === 0 ||
            bulkTargets.length > MAX_BULK}
          on:click={runBulk}>{bulkRunning ? 'Updating…' : 'Update'}</button
        >
      {/if}
    </svelte:fragment>
  </Modal>
{/if}

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
  .header-actions {
    display: flex;
    gap: 0.5rem;
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
  .select-cell {
    width: 1px;
    padding-right: 0;
  }
  .credential-age {
    color: var(--text-muted);
    white-space: nowrap;
  }
  .credential-stale {
    color: var(--warning);
    font-weight: 600;
    cursor: help;
  }
  .auth-choice {
    display: flex;
    gap: 1rem;
  }
  .auth-choice label,
  .check-field {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .mono {
    font-family: var(--font-mono);
  }
  .bulk-summary {
    margin: 0;
    font-weight: 600;
  }
  .bulk-problems {
    margin: 0;
    padding-left: 1.1rem;
    font-size: 0.85rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .bulk-status {
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    margin-right: 0.4rem;
  }
  .bulk-failed {
    color: var(--danger);
  }
  .bulk-skipped {
    color: var(--text-faint);
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
