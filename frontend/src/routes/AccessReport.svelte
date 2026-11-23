<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import {
    ALL_FLAGS,
    DEFAULT_INACTIVE_DAYS,
    FLAG_HINTS,
    FLAG_LABELS,
    MAX_INACTIVE_DAYS,
    MIN_INACTIVE_DAYS,
    filterSources,
    filterUsers,
    flagHint,
    flagText,
    groupAccess,
    holdersByCapability,
    jsonExportName,
    loginMethod,
    mfaText,
    parseInactiveDays,
    placeText,
    reportUrl,
    viaText,
  } from '../lib/access-report'
  import { formatAuditTime } from '../lib/audit-format'
  import { CAPABILITY_HINTS, CAPABILITY_LABELS, GLOBAL_CAPABILITY_SHORT_LABELS } from '../lib/capabilities'
  import AccessReportGroup from '../lib/components/AccessReportGroup.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type {
    AccessReportByUser,
    AccessReportBySource,
    AccessReportFlag,
    AccessReportHeader,
    AccessReportView,
  } from '../lib/types'

  // Settings -> Access review (#384). Everything shown comes from
  // GET /access-report, which decides what this user may see; each load and
  // each export is generated, and audited, by the server.

  let view: AccessReportView = 'users'
  let inactiveDays = DEFAULT_INACTIVE_DAYS
  let daysInput: number | string | null = DEFAULT_INACTIVE_DAYS
  let daysError = ''

  let header: AccessReportHeader | null = null
  let byUser: AccessReportByUser | null = null
  let bySource: AccessReportBySource | null = null
  let loading = true
  let error = ''
  let request = 0

  let userQuery = ''
  let selectedFlags = new Set<AccessReportFlag>()
  let expanded = new Set<number>()
  let accessQueries: Record<number, string> = {}
  let sourceQuery = ''

  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone

  async function load() {
    const mine = ++request
    loading = true
    error = ''
    try {
      const report = await api.get<AccessReportByUser | AccessReportBySource>(
        reportUrl(view, 'json', inactiveDays),
      )
      if (mine !== request) return
      header = report
      if ('users' in report) {
        byUser = report
        bySource = null
      } else {
        bySource = report
        byUser = null
      }
    } catch (err) {
      if (mine !== request) return
      header = null
      byUser = null
      bySource = null
      error = err instanceof ApiError ? err.detail : 'Failed to load the access report'
    } finally {
      if (mine === request) loading = false
    }
  }

  function showView(next: AccessReportView) {
    if (next === view) return
    view = next
    load()
  }

  function applyDays() {
    const days = parseInactiveDays(daysInput)
    if (days === null) {
      daysError = `Enter a whole number of days between ${MIN_INACTIVE_DAYS} and ${MAX_INACTIVE_DAYS}.`
      return
    }
    daysError = ''
    if (days === inactiveDays) return
    inactiveDays = days
    load()
  }

  function toggleFlag(flag: AccessReportFlag) {
    const next = new Set(selectedFlags)
    if (next.has(flag)) next.delete(flag)
    else next.add(flag)
    selectedFlags = next
  }

  function toggleUser(id: number) {
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    expanded = next
  }

  $: users = byUser ? filterUsers(byUser.users, selectedFlags, userQuery) : []
  $: sources = bySource ? filterSources(bySource.sources, sourceQuery) : []

  onMount(load)
</script>

<SettingsNav />

<div class="page">
  <h1>Access review</h1>
  <p class="hint">
    Who can reach which sources, and with which capabilities, as PerchTail enforces it now: each
    user's role, sign-in method, last sign-in and two-factor state, and the grant behind every
    access. Only active users are listed. Viewing or exporting the report is recorded in the audit
    log. Times are shown in your time zone ({browserZone}).
  </p>

  {#if header}
    <p class="generated">
      Generated at {formatAuditTime(header.generated_at)} by {header.generated_by.username}
    </p>
    {#if header.scope === 'delegated'}
      <p class="scope-note" role="note">
        This report is limited to your scope: the sources you hold access on, the users who hold
        something there, and only the access they have there.
      </p>
    {/if}
  {/if}

  <div class="toolbar card">
    <div class="view-switch" role="group" aria-label="Report view">
      <button
        type="button"
        class="btn"
        class:btn-primary={view === 'users'}
        class:btn-ghost={view !== 'users'}
        aria-pressed={view === 'users'}
        on:click={() => showView('users')}>By user</button
      >
      <button
        type="button"
        class="btn"
        class:btn-primary={view === 'sources'}
        class:btn-ghost={view !== 'sources'}
        aria-pressed={view === 'sources'}
        on:click={() => showView('sources')}>By source</button
      >
    </div>

    <label class="days">
      Days without sign-in
      <input
        class="input"
        type="number"
        min={MIN_INACTIVE_DAYS}
        max={MAX_INACTIVE_DAYS}
        step="1"
        bind:value={daysInput}
        on:change={applyDays}
        aria-invalid={daysError ? 'true' : undefined}
        aria-describedby={daysError ? 'days-error' : undefined}
      />
    </label>

    <div class="exports">
      <a class="btn btn-ghost" href={reportUrl(view, 'csv', inactiveDays)} download>Export CSV</a>
      <a class="btn btn-ghost" href={reportUrl(view, 'json', inactiveDays)} download={jsonExportName(view)}
        >Export JSON</a
      >
    </div>
  </div>
  {#if daysError}
    <p class="error" id="days-error">{daysError}</p>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {:else if loading && !header}
    <p class="hint">Loading…</p>
  {:else if view === 'users' && byUser}
    <div class="filters">
      <input
        class="input filter-text"
        type="search"
        placeholder="Filter users…"
        aria-label="Filter users"
        bind:value={userQuery}
      />
      <div class="flag-filters" role="group" aria-label="Filter by flag">
        {#each ALL_FLAGS as flag (flag)}
          <label class="chip" class:chip-active={selectedFlags.has(flag)} title={FLAG_HINTS[flag]}>
            <input type="checkbox" checked={selectedFlags.has(flag)} on:change={() => toggleFlag(flag)} />
            {FLAG_LABELS[flag]}
          </label>
        {/each}
      </div>
    </div>

    {#if users.length === 0}
      <p class="hint">No matching users.</p>
    {:else}
      <div class="table-wrap card">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              <th>Sign-in</th>
              <th>Last sign-in ({browserZone})</th>
              <th>MFA</th>
              <th>Global capabilities</th>
              <th>Flags</th>
              <th><span class="visually-hidden">Access</span></th>
            </tr>
          </thead>
          <tbody>
            {#each users as user (user.id)}
              {@const open = expanded.has(user.id)}
              <tr class="user-row">
                <td>
                  <span class="username">{user.username}</span>
                  {#if user.display_name}<span class="display-name">{user.display_name}</span>{/if}
                </td>
                <td>{user.role.name}</td>
                <td>{loginMethod(user)}</td>
                <td class="nowrap">{user.last_login_at ? formatAuditTime(user.last_login_at) : 'Never'}</td>
                <td>{mfaText(user.mfa)}</td>
                <td class="badges">
                  {#each user.global_capabilities as cap (cap)}
                    <span class="badge badge-muted">{GLOBAL_CAPABILITY_SHORT_LABELS[cap]}</span>
                  {:else}—{/each}
                </td>
                <td class="badges">
                  {#each user.flags as flag (flag)}
                    <span class="badge badge-warning flag" title={flagHint(flag, user)}
                      >{flagText(flag, user, header?.parameters.inactive_days ?? inactiveDays)}</span
                    >
                  {:else}—{/each}
                </td>
                <td>
                  <button
                    type="button"
                    class="btn btn-ghost btn-sm"
                    aria-expanded={open}
                    aria-label={`${open ? 'Hide' : 'Show'} access of ${user.username}`}
                    on:click={() => toggleUser(user.id)}
                  >
                    {open ? 'Hide access' : `Access (${user.access.length})`}
                  </button>
                </td>
              </tr>
              {#if open}
                {@const query = accessQueries[user.id] ?? ''}
                <tr class="access-row">
                  <td colspan="8">
                    {#if user.access.length === 0}
                      <p class="hint">No access to any source in this report.</p>
                    {:else}
                      <input
                        class="input access-search"
                        type="search"
                        placeholder="Search customers, folders and sources…"
                        aria-label={`Search the access of ${user.username}`}
                        bind:value={accessQueries[user.id]}
                      />
                      {#each groupAccess(user.access, query) as group (group.key)}
                        <AccessReportGroup {group} expand={query.trim() !== ''} />
                      {:else}
                        <p class="hint">No matching sources.</p>
                      {/each}
                    {/if}
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {:else if view === 'sources' && bySource}
    <div class="filters">
      <input
        class="input filter-text"
        type="search"
        placeholder="Search sources…"
        aria-label="Search sources"
        bind:value={sourceQuery}
      />
    </div>

    {#each sources as source (source.source_id)}
      <section class="source card" aria-label={source.source_name}>
        <header class="source-header">
          <h2>{source.source_name}</h2>
          <span class="place">{placeText(source)}</span>
          <span class="count">{source.users.length} {source.users.length === 1 ? 'user' : 'users'}</span>
        </header>
        {#if source.users.length === 0}
          <p class="hint">Nobody in this report holds anything on this source.</p>
        {:else}
          <table>
            <thead>
              <tr>
                <th>Capability</th>
                <th>User</th>
                <th>Role</th>
                <th>Granted by</th>
              </tr>
            </thead>
            <tbody>
              {#each holdersByCapability(source) as row (row.capability)}
                {#each row.holders as holder, i (holder.id)}
                  <tr>
                    {#if i === 0}
                      <th scope="rowgroup" rowspan={row.holders.length} class="cap" title={CAPABILITY_HINTS[row.capability]}
                        >{CAPABILITY_LABELS[row.capability]}</th
                      >
                    {/if}
                    <td>
                      <span class="username">{holder.username}</span>
                      {#if holder.display_name}<span class="display-name">{holder.display_name}</span>{/if}
                    </td>
                    <td>{holder.role.name}</td>
                    <td class="via">
                      {#each holder.via as v, j (j)}<span>{viaText(v)}</span>{/each}
                    </td>
                  </tr>
                {/each}
              {/each}
            </tbody>
          </table>
        {/if}
      </section>
    {:else}
      <p class="hint">No matching sources.</p>
    {/each}
  {/if}
</div>

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
  h2 {
    font-size: 0.95rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .generated {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .scope-note {
    margin: 0;
    padding: 0.6rem 1rem;
    border-radius: 8px;
    background: var(--warning-soft);
    color: var(--text);
    font-size: 0.85rem;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 1.5rem;
    padding: 1rem 1.5rem;
  }
  .view-switch {
    display: flex;
    gap: 0.4rem;
  }
  .days {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .days input {
    width: 8rem;
  }
  .exports {
    display: flex;
    gap: 0.5rem;
    margin-left: auto;
  }
  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
  }
  .filter-text {
    width: 18rem;
  }
  .flag-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.25rem 0.65rem;
    font-size: 0.75rem;
    color: var(--text-muted);
    cursor: pointer;
  }
  .chip input {
    accent-color: var(--accent);
  }
  .chip-active {
    border-color: var(--accent-border);
    background: var(--accent-soft);
    color: var(--accent-hover);
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
    vertical-align: top;
    padding: 0.55rem 0.9rem;
    border-bottom: 1px solid var(--border-soft);
    color: var(--text);
  }
  thead th {
    color: var(--text-faint);
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }
  .nowrap {
    white-space: nowrap;
  }
  .username {
    display: block;
    font-weight: 500;
  }
  .display-name {
    display: block;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .badges {
    max-width: 18rem;
  }
  .badges .badge {
    display: inline-block;
    margin: 0 0.25rem 0.25rem 0;
  }
  .badge-muted {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .badge-warning {
    background: var(--warning-soft);
    color: var(--text);
  }
  .access-row td {
    background: var(--bg);
    padding: 0.75rem 1.5rem 1rem;
  }
  .access-search {
    width: 22rem;
    margin-bottom: 0.5rem;
  }
  .source {
    padding: 1rem 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .source-header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  .place,
  .count {
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .cap {
    font-weight: 600;
    white-space: nowrap;
  }
  .via span {
    display: block;
    font-size: 0.78rem;
    color: var(--text-muted);
  }
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
