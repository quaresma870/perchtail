<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import {
    anyUpgrading,
    bulkSummary,
    bulkTargets,
    DAY_NAMES,
    normalizeStart,
    runDuration,
    runRequester,
    runStatusText,
    sourcePolicyLabel,
    statusLabel,
    statusTone,
    SYSTEM_POLICY_LABELS,
    upgradeResultText,
    windowText,
  } from '../lib/agent-upgrade'
  import { confirmAction } from '../lib/confirm'
  import { parseUtc } from '../lib/credential-age'
  import Modal from '../lib/components/Modal.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { ago } from '../lib/source-status'
  import type {
    AgentRow,
    AgentUpgradeHistory,
    AgentUpgradeOverview,
    AgentUpgradePolicy,
    AgentUpgradeStarted,
    AgentUpgradeSystemPolicy,
    BulkAgentUpgradeResult,
  } from '../lib/types'

  /** Settings → Agents (#319): the remote-upgrade policy and window for
   * every push agent, and one row per agent with its own policy, its last
   * upgrade and the actions. Only for users who can manage sources. */

  let overview: AgentUpgradeOverview | null = null
  let rows: AgentRow[] = []
  let loading = true
  let error = ''
  let now = new Date()

  // The settings form, a copy of the saved values until saved.
  let policy: AgentUpgradeSystemPolicy = 'off'
  let days: number[] = []
  let start = '02:00'
  let hours = 4
  let failureLimit = 3
  let concurrency = 5
  let saving = false
  let saveError = ''
  let saved = ''

  function fillForm(o: AgentUpgradeOverview) {
    policy = o.policy
    days = [...o.window_days]
    start = o.window_start
    hours = o.window_hours
    failureLimit = o.failure_limit
    concurrency = o.concurrency
  }

  $: dirty =
    overview !== null &&
    (policy !== overview.policy ||
      [...days].sort().join(',') !== [...overview.window_days].sort().join(',') ||
      start !== overview.window_start ||
      hours !== overview.window_hours ||
      failureLimit !== overview.failure_limit ||
      concurrency !== overview.concurrency)
  $: targets = bulkTargets(rows)
  $: showBulk = overview !== null && (overview.policy !== 'off' || targets.length > 0)

  async function load() {
    try {
      const [o, r] = await Promise.all([
        api.get<AgentUpgradeOverview>('/agent-upgrades'),
        api.get<AgentRow[]>('/agent-upgrades/agents'),
      ])
      const first = overview === null
      overview = o
      rows = r
      if (first) fillForm(o)
      error = ''
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load the agents'
    } finally {
      loading = false
      now = new Date()
    }
    schedulePoll()
  }

  // Poll while an upgrade is under way, so its progress shows.
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  function schedulePoll() {
    if (pollTimer !== null || !anyUpgrading(rows)) return
    pollTimer = setTimeout(() => {
      pollTimer = null
      load()
      if (historyFor) loadHistory()
    }, 3000)
  }

  async function save() {
    saveError = ''
    saved = ''
    const normalized = normalizeStart(start)
    if (normalized === null) {
      saveError = 'Use HH:MM for the start time, for example 02:00'
      return
    }
    if (days.length === 0) {
      saveError = 'Pick at least one day for the window'
      return
    }
    saving = true
    try {
      overview = await api.patch<AgentUpgradeOverview>('/agent-upgrades/settings', {
        policy,
        window_days: days,
        window_start: normalized,
        window_hours: hours,
        failure_limit: failureLimit,
        concurrency,
      })
      fillForm(overview)
      saved = 'Saved.'
      rows = await api.get<AgentRow[]>('/agent-upgrades/agents')
    } catch (err) {
      saveError = err instanceof ApiError ? err.detail : 'Failed to save'
    } finally {
      saving = false
    }
  }

  // --- the rows -------------------------------------------------------------

  let rowError: Record<number, string> = {}
  let busy: Record<number, boolean> = {}

  function replaceRow(updated: AgentRow) {
    rows = rows.map((r) => (r.source_id === updated.source_id ? updated : r))
  }

  async function setPolicy(row: AgentRow, value: AgentUpgradePolicy) {
    rowError = { ...rowError, [row.source_id]: '' }
    try {
      replaceRow(await api.put<AgentRow>(`/sources/${row.source_id}/agent-upgrade-policy`, { policy: value }))
    } catch (err) {
      rowError = { ...rowError, [row.source_id]: err instanceof ApiError ? err.detail : 'Failed to save' }
      rows = [...rows] // puts the dropdown back
    }
  }

  async function upgrade(row: AgentRow) {
    const ok = await confirmAction({
      title: 'Upgrade agent',
      message: `Upgrade the agent of "${row.name}" from ${row.agent_version} to ${overview?.version ?? "this server's version"}? It downloads the signed release, checks it, replaces its binary and restarts. If anything fails, the current version keeps running.`,
      confirmLabel: 'Upgrade',
    })
    if (!ok) return
    busy = { ...busy, [row.source_id]: true }
    rowError = { ...rowError, [row.source_id]: '' }
    try {
      const started = await api.post<AgentUpgradeStarted>(`/sources/${row.source_id}/agent-upgrade`)
      if (started.result !== 'accepted') {
        rowError = { ...rowError, [row.source_id]: upgradeResultText(started.result, started.detail) }
      }
    } catch (err) {
      rowError = { ...rowError, [row.source_id]: err instanceof ApiError ? err.detail : 'Failed to start the upgrade' }
    } finally {
      busy = { ...busy, [row.source_id]: false }
    }
    await load()
  }

  async function resume(row: AgentRow) {
    const ok = await confirmAction({
      title: 'Resume automatic upgrades',
      message: `Clear the ${row.failures} failed upgrade${row.failures === 1 ? '' : 's'} of "${row.name}", so automatic upgrades include it again?`,
      confirmLabel: 'Resume',
    })
    if (!ok) return
    try {
      replaceRow(await api.post<AgentRow>(`/sources/${row.source_id}/agent-upgrade/resume`))
    } catch (err) {
      rowError = { ...rowError, [row.source_id]: err instanceof ApiError ? err.detail : 'Failed to resume' }
    }
  }

  // --- upgrade all ------------------------------------------------------------

  let bulkRunning = false
  let bulkResults: BulkAgentUpgradeResult[] | null = null
  let bulkError = ''
  $: bulk = bulkResults ? bulkSummary(bulkResults) : null

  async function upgradeAll() {
    const count = targets.length
    const ok = await confirmAction({
      title: 'Upgrade all outdated agents',
      message: `Upgrade the ${count} connected outdated agent${count === 1 ? '' : 's'} whose policy allows it to ${overview?.version ?? "this server's version"}? Suspended agents and agents set to Off are left out. Each one downloads the signed release, checks it, replaces its binary and restarts; if anything fails, it keeps running its current version.`,
      confirmLabel: 'Upgrade',
    })
    if (!ok) return
    bulkRunning = true
    bulkError = ''
    bulkResults = null
    try {
      bulkResults = await api.post<BulkAgentUpgradeResult[]>('/agent-upgrades', {})
    } catch (err) {
      bulkError = err instanceof ApiError ? err.detail : 'Failed to start the upgrades'
    } finally {
      bulkRunning = false
    }
    await load()
  }

  // --- history -------------------------------------------------------------

  const PAGE = 10
  let historyFor: AgentRow | null = null
  let history: AgentUpgradeHistory | null = null
  let historyOffset = 0
  let historyError = ''

  function openHistory(row: AgentRow) {
    historyFor = row
    historyOffset = 0
    history = null
    loadHistory()
  }

  async function loadHistory() {
    if (!historyFor) return
    const id = historyFor.source_id
    try {
      const page = await api.get<AgentUpgradeHistory>(
        `/sources/${id}/agent-upgrades?offset=${historyOffset}&limit=${PAGE}`,
      )
      if (historyFor?.source_id === id) history = page
      historyError = ''
    } catch (err) {
      historyError = err instanceof ApiError ? err.detail : 'Failed to load the history'
    }
  }

  function page(delta: number) {
    historyOffset = Math.max(0, historyOffset + delta * PAGE)
    loadHistory()
  }

  function when(iso: string | null): string {
    return iso ? parseUtc(iso).toLocaleString() : ''
  }

  onMount(load)
  onDestroy(() => {
    if (pollTimer !== null) clearTimeout(pollTimer)
  })
</script>

<SettingsNav />

<div class="page">
  <h1>Agents</h1>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else if error && !overview}
    <p class="error">{error}</p>
  {:else if overview}
    <section class="card top">
      <div class="settings">
        <label class="field">
          Remote upgrades
          <select class="input" bind:value={policy} aria-label="Remote upgrades">
            {#each Object.entries(SYSTEM_POLICY_LABELS) as [value, label] (value)}
              <option {value}>{label}</option>
            {/each}
          </select>
          <span class="hint">
            Off: no upgrades from here. Manual: whoever can manage sources upgrades agents from this page.
            Automatic: also upgraded inside the window. Each agent can follow this or have its own policy below.
          </span>
        </label>

        <fieldset class="field window">
          <legend>Automatic window ({overview.timezone})</legend>
          <div class="days">
            {#each DAY_NAMES as name, index (name)}
              <label class="day">
                <input type="checkbox" bind:group={days} value={index} aria-label={name} />
                {name}
              </label>
            {/each}
          </div>
          <div class="inline">
            <label>
              Start
              <input class="input time" type="time" step="60" bind:value={start} aria-label="Window start" />
            </label>
            <label>
              for
              <input
                class="input number"
                type="number"
                min="1"
                max="24"
                bind:value={hours}
                aria-label="Window length in hours"
              />
              hours
            </label>
          </div>
          <span class="hint">
            In the server's time zone, {overview.timezone}. A window may run past midnight.
            {#if overview.window_open}
              The window is open now.
            {:else if overview.next_window_start}
              Next window: {when(overview.next_window_start)} (your time).
            {/if}
          </span>
        </fieldset>

        <label class="field">
          Failure limit
          <input
            class="input number"
            type="number"
            min="1"
            max="10"
            bind:value={failureLimit}
            aria-label="Failure limit"
          />
          <span class="hint">After this many failed upgrades in a row, automatic upgrades skip the agent until it's resumed.</span>
        </label>

        <label class="field">
          At once
          <input
            class="input number"
            type="number"
            min="1"
            max="50"
            bind:value={concurrency}
            aria-label="Agents upgraded at once"
          />
          <span class="hint">How many agents the schedule upgrades at the same time.</span>
        </label>
      </div>

      <div class="actions">
        <button class="btn btn-primary" disabled={!dirty || saving} on:click={save}>{saving ? 'Saving…' : 'Save'}</button>
        {#if saveError}<span class="error">{saveError}</span>{/if}
        {#if saved && !dirty}<span class="hint">{saved}</span>{/if}
        <span class="spacer"></span>
        {#if showBulk}
          <button
            class="btn btn-ghost"
            disabled={bulkRunning || targets.length === 0}
            title={targets.length === 0
              ? 'No connected outdated agent whose policy allows it'
              : "Upgrade every connected outdated agent whose policy allows it to this server's version"}
            on:click={upgradeAll}
            >{bulkRunning ? 'Upgrading…' : `Upgrade all outdated agents (${targets.length})`}</button
          >
        {/if}
      </div>

      <div class="facts">
        <p>
          Agents are upgraded to this server's version, <strong>{overview.version}</strong>.
          {#if overview.policy === 'automatic'}Automatic window: {windowText(overview)}.{/if}
        </p>
        {#if !overview.bundled}
          <p class="hint">
            This server doesn't carry the agent binaries ({overview.bundled_reason}): agents download
            {overview.version} from GitHub instead, so their hosts need to reach github.com.
          </p>
        {/if}
        {#if overview.last_window}
          <p class="summary" data-testid="last-window">
            Last automatic window, {when(overview.last_window.start)} to {when(overview.last_window.end)}:
            {overview.last_window.upgraded} upgraded, {overview.last_window.failed} failed,
            {overview.last_window.suspended} suspended{overview.last_window.running
              ? `, ${overview.last_window.running} still running`
              : ''}.
          </p>
        {:else}
          <p class="hint">No automatic window has run yet.</p>
        {/if}
        <p class="hint">
          A host can always refuse with PERCHTAIL_REMOTE_UPGRADE=false. Agents older than 0.7 are upgraded by hand once.
        </p>
      </div>
    </section>

    {#if bulkError}
      <p class="error" role="status">{bulkError}</p>
    {/if}
    {#if bulk}
      <div class="notice" role="status">
        {bulk.text}
        {#if bulk.problems.length > 0}
          <ul>
            {#each bulk.problems as result (result.source_id)}
              <li><strong>{result.name ?? `#${result.source_id}`}</strong>{result.detail ? `: ${result.detail}` : ''}</li>
            {/each}
          </ul>
        {/if}
        <button class="link" on:click={() => (bulkResults = null)}>Dismiss</button>
      </div>
    {/if}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="card">
      {#if rows.length === 0}
        <p class="empty">No push-agent sources yet.</p>
      {:else}
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Connection</th>
              <th>Agent</th>
              <th>Server</th>
              <th>Policy</th>
              <th>Last result</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each rows as row (row.source_id)}
              <tr data-source={row.name}>
                <td>
                  <a class="name-link" href={`#/settings/sources/${row.source_id}`}>{row.name}</a>
                  {#if row.customer}<div class="sub">{row.customer}</div>{/if}
                </td>
                <td>
                  {#if !row.enabled}
                    <span class="status status-pending">disabled</span>
                  {:else if row.upgrading}
                    <span class="status status-pending">⟳ upgrading</span>
                  {:else if row.connected}
                    <span class="status status-ok">✓ connected</span>
                  {:else}
                    <span class="status status-fail">not connected</span>
                  {/if}
                </td>
                <td class:outdated={row.outdated}>{row.agent_version ?? 'unknown'}</td>
                <td>{overview.version}</td>
                <td>
                  <select
                    class="input policy"
                    aria-label={`Upgrade policy of ${row.name}`}
                    value={row.policy}
                    on:change={(e) => setPolicy(row, e.currentTarget.value as AgentUpgradePolicy)}
                  >
                    <option value="inherit">{sourcePolicyLabel('inherit', overview.policy)}</option>
                    <option value="off">Off</option>
                    <option value="manual">Manual</option>
                    <option value="automatic">Automatic</option>
                  </select>
                  {#if row.suspended}
                    <div class="sub suspended" title="Automatic upgrades skip this agent until it's resumed">
                      suspended after {row.failures} failures
                    </div>
                  {/if}
                </td>
                <td>
                  {#if row.last_run}
                    <button
                      class="link result result-{statusTone(row.last_run.status)}"
                      title={runStatusText(row.last_run)}
                      on:click={() => openHistory(row)}
                    >
                      {statusLabel(row.last_run.status)}
                    </button>
                    <div class="sub">{ago(row.last_run.started_at, now)}</div>
                  {:else}
                    <span class="hint">—</span>
                  {/if}
                </td>
                <td>
                  <div class="row-actions">
                    {#if row.suspended}
                      <button class="btn btn-ghost btn-sm" on:click={() => resume(row)}>Resume</button>
                    {/if}
                    {#if row.outdated}
                      <button
                        class="btn btn-ghost btn-sm"
                        disabled={busy[row.source_id] || row.blocker !== null}
                        title={row.blocker ?? `Upgrade the agent to ${overview.version}`}
                        on:click={() => upgrade(row)}>{busy[row.source_id] ? 'Asking…' : 'Upgrade'}</button
                      >
                    {/if}
                  </div>
                  {#if rowError[row.source_id]}
                    <div class="sub error" role="status">{rowError[row.source_id]}</div>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/if}
</div>

{#if historyFor}
  <Modal title={`Upgrades of ${historyFor.name}`} wide on:close={() => (historyFor = null)}>
    {#if historyError}
      <p class="error">{historyError}</p>
    {:else if !history}
      <p class="hint">Loading…</p>
    {:else if history.items.length === 0}
      <p class="hint">No upgrade yet.</p>
    {:else}
      <table class="history">
        <thead>
          <tr>
            <th>Started</th>
            <th>Asked by</th>
            <th>Versions</th>
            <th>Result</th>
            <th>Duration</th>
          </tr>
        </thead>
        <tbody>
          {#each history.items as item (item.id)}
            <tr>
              <td>{when(item.started_at)}</td>
              <td>{runRequester(item)}</td>
              <td>{item.from_version ?? '?'} → {item.to_version}</td>
              <td>
                <span class="result-{statusTone(item.status)}">{statusLabel(item.status)}</span>
                {#if item.message}<div class="sub">{item.message}</div>{/if}
                {#if item.suspended_source}<div class="sub suspended">suspended automatic upgrades</div>{/if}
              </td>
              <td>{runDuration(item)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      <div class="pager">
        <button class="btn btn-ghost btn-sm" disabled={historyOffset === 0} on:click={() => page(-1)}>Newer</button>
        <span class="hint">
          {historyOffset + 1}–{historyOffset + history.items.length} of {history.total}
        </span>
        <button
          class="btn btn-ghost btn-sm"
          disabled={historyOffset + history.items.length >= history.total}
          on:click={() => page(1)}>Older</button
        >
      </div>
    {/if}
  </Modal>
{/if}

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
  .top {
    padding: 1.1rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .settings {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 1rem 1.5rem;
    align-items: start;
  }
  .window {
    border: none;
    margin: 0;
    padding: 0;
  }
  .window legend {
    padding: 0;
    margin-bottom: 0.3rem;
  }
  .days {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.7rem;
    color: var(--text);
  }
  .day {
    display: inline-flex;
    gap: 0.25rem;
    align-items: center;
  }
  .inline {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: center;
    color: var(--text);
  }
  .inline label {
    display: inline-flex;
    gap: 0.4rem;
    align-items: center;
  }
  .time {
    width: 7rem;
  }
  .number {
    width: 5rem;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
  }
  .spacer {
    flex: 1;
  }
  .facts {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.86rem;
    color: var(--text);
  }
  .facts p {
    margin: 0;
  }
  .hint {
    font-size: 0.82rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
  .notice {
    padding: 0.6rem 0.9rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 0.86rem;
  }
  .notice ul {
    margin: 0.3rem 0;
    padding-left: 1.1rem;
  }
  .card {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 0.65rem 1rem;
    font-size: 0.88rem;
    vertical-align: top;
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
  .history th,
  .history td {
    padding: 0.45rem 0.6rem;
  }
  .sub {
    color: var(--text-faint);
    font-size: 0.78rem;
    margin-top: 0.15rem;
  }
  .sub.error {
    color: var(--danger);
  }
  .sub.suspended,
  .outdated {
    color: var(--warning);
  }
  .policy {
    width: auto;
    min-width: 9rem;
  }
  .status {
    font-size: 0.82rem;
    font-weight: 600;
  }
  .status-ok,
  .result-ok {
    color: var(--success);
  }
  .status-fail,
  .result-fail {
    color: var(--danger);
  }
  .status-pending,
  .result-pending {
    color: var(--text-faint);
  }
  button.link {
    border: none;
    background: none;
    cursor: pointer;
    padding: 0;
    font: inherit;
    font-weight: 600;
    color: var(--accent-hover);
  }
  button.link.result-ok {
    color: var(--success);
  }
  button.link.result-fail {
    color: var(--danger);
  }
  button.link.result-pending {
    color: var(--text-muted);
  }
  .name-link {
    color: var(--text);
    font-weight: 600;
    text-decoration: none;
  }
  .name-link:hover {
    text-decoration: underline;
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 2rem;
  }
  .pager {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 0.75rem;
  }
</style>
