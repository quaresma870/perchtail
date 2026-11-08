<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import { currentUser, hasCapability } from '../lib/auth'
  import { confirmAction } from '../lib/confirm'
  import { buildConnectionTree, type ConnectionTreeNode } from '../lib/connection-tree'
  import type { Alert, AlertCreate, AlertTestResult, Source } from '../lib/types'

  const DEFAULT_FILE_GLOB = '**/*.log'
  // Non-breaking, so the indentation survives inside an <option>.
  const INDENT = '   '

  let alerts: Alert[] = []
  let sources: Source[] = []
  let loading = true
  let error = ''

  // One form for both creating and editing (editingId set).
  let showForm = false
  let editingId: number | null = null
  let saving = false
  let formError = ''
  let name = ''
  let query = ''
  let regex = false
  let scope = ''
  let fileGlob = DEFAULT_FILE_GLOB
  let webhookUrl = ''

  let testResult: Record<number, 'sending' | AlertTestResult> = {}

  // Without manage_alerts you can still see and delete your own alerts, but
  // not create, edit, enable or test them (the API enforces the same).
  $: canManage = hasCapability($currentUser, 'manage_alerts')

  interface ScopeOption {
    key: string
    label: string
    path: string
  }

  // Customers and folders come from the sources the user can view (each
  // carries its customer and folder path): /customers and /folders are admin
  // endpoints, and a scope with no visible source can't be watched anyway.
  function scopeOptions(tree: ConnectionTreeNode[]): ScopeOption[] {
    const options: ScopeOption[] = []
    function walk(node: ConnectionTreeNode, depth: number, path: string[]) {
      const here = [...path, node.name]
      if (node.id !== 'unassigned') {
        const kind = node.id.startsWith('customer:') ? 'customer' : 'folder'
        options.push({
          key: node.id,
          label: `${INDENT.repeat(depth)}${node.name} (${kind})`,
          path: here.join(' / '),
        })
      }
      const childDepth = node.id === 'unassigned' ? depth : depth + 1
      for (const child of node.children) walk(child, childDepth, here)
      for (const source of node.sources) {
        options.push({
          key: `source:${source.id}`,
          label: `${INDENT.repeat(childDepth)}${source.name}`,
          path: source.name,
        })
      }
    }
    for (const node of tree) walk(node, 0, [])
    return options
  }

  $: options = scopeOptions(buildConnectionTree(sources))
  $: optionsByKey = new Map(options.map((o) => [o.key, o]))

  function scopeKeyOf(alert: Alert): string {
    if (alert.source_id !== null) return `source:${alert.source_id}`
    if (alert.folder_id !== null) return `folder:${alert.folder_id}`
    if (alert.customer_id !== null) return `customer:${alert.customer_id}`
    return ''
  }

  function scopeLabel(key: string): string {
    if (key === '') return 'Every source you can view'
    const [kind, id] = key.split(':')
    return optionsByKey.get(key)?.path ?? `${kind} #${id}`
  }

  // Exactly one scope field, or source_id: null for every source (on a
  // PATCH, any scope field replaces the whole scope).
  function scopePayload(key: string): Pick<AlertCreate, 'source_id' | 'folder_id' | 'customer_id'> {
    const [kind, id] = key.split(':')
    if (kind === 'source') return { source_id: Number(id) }
    if (kind === 'folder') return { folder_id: Number(id) }
    if (kind === 'customer') return { customer_id: Number(id) }
    return { source_id: null }
  }

  function deliveryWarning(alert: Alert): string {
    const parts: string[] = []
    const n = alert.failed_deliveries
    if (n > 0) parts.push(`${n} failed ${n === 1 ? 'delivery' : 'deliveries'}`)
    if (alert.last_error) parts.push(`last error: ${alert.last_error}`)
    return parts.join(' · ')
  }

  const formatTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : 'never')

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

  function openCreate() {
    editingId = null
    name = ''
    query = ''
    regex = false
    scope = ''
    fileGlob = DEFAULT_FILE_GLOB
    webhookUrl = ''
    formError = ''
    showForm = true
  }

  function openEdit(alert: Alert) {
    editingId = alert.id
    name = alert.name
    query = alert.query
    regex = alert.regex
    scope = scopeKeyOf(alert)
    fileGlob = alert.file_glob
    webhookUrl = alert.webhook_url
    formError = ''
    showForm = true
  }

  function closeForm() {
    showForm = false
    editingId = null
  }

  async function saveAlert() {
    formError = ''
    saving = true
    try {
      const fields = { name, query, regex, file_glob: fileGlob, webhook_url: webhookUrl }
      if (editingId === null) {
        const payload: AlertCreate = { ...fields, ...scopePayload(scope) }
        await api.post<Alert>('/alerts', payload)
      } else {
        const original = alerts.find((a) => a.id === editingId)
        // Only what changed, so the audit log names the right fields.
        const payload: Partial<AlertCreate> = Object.fromEntries(
          Object.entries(fields).filter(([key, value]) => original?.[key as keyof Alert] !== value),
        )
        if (!original || scopeKeyOf(original) !== scope) Object.assign(payload, scopePayload(scope))
        if (Object.keys(payload).length > 0) await api.patch<Alert>(`/alerts/${editingId}`, payload)
      }
      closeForm()
      await load()
    } catch (err) {
      formError = err instanceof ApiError ? err.detail : 'Failed to save alert'
    } finally {
      saving = false
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
    const ok = await confirmAction({
      title: 'Delete alert',
      message: `Delete the alert "${alert.name}"? This can't be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    try {
      await api.delete(`/alerts/${alert.id}`)
      if (editingId === alert.id) closeForm()
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete alert'
    }
  }

  async function sendTest(alert: Alert) {
    testResult = { ...testResult, [alert.id]: 'sending' }
    try {
      const result = await api.post<AlertTestResult>(`/alerts/${alert.id}/test`)
      testResult = { ...testResult, [alert.id]: result }
    } catch (err) {
      const message = err instanceof ApiError ? err.detail : 'Failed to send the test'
      testResult = { ...testResult, [alert.id]: { ok: false, error: message } }
    }
  }

  onMount(load)
</script>

<div class="page">
  <div class="header">
    <div>
      <h1>Alerts</h1>
      <p class="hint">
        Get a webhook notification when a new line matching a query is written to the files an
        alert watches. Alerts read only the lines added since their last check, at the interval set
        in Settings → System.
      </p>
    </div>
    {#if canManage}
      <button class="btn btn-primary" on:click={() => (showForm ? closeForm() : openCreate())}>
        {showForm ? 'Cancel' : '+ New alert'}
      </button>
    {/if}
  </div>

  {#if showForm}
    <form class="card alert-form" on:submit|preventDefault={saveAlert}>
      <h2>{editingId === null ? 'New alert' : 'Edit alert'}</h2>
      {#if formError}
        <p class="error">{formError}</p>
      {/if}
      <label>
        Name
        <input class="input" bind:value={name} required />
      </label>
      <label>
        Query
        <input
          class="input mono"
          bind:value={query}
          placeholder={regex ? 'e.g. timeout after \\d+ ms' : 'e.g. connection refused'}
          required
        />
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={regex} />
        Regular expression
      </label>
      <p class="hint field-hint">
        {regex
          ? 'An RE2 regular expression (no backreferences or lookaround), matched anywhere in a line.'
          : 'Plain text, matched anywhere in a line, ignoring case.'}
      </p>
      <label>
        Watch
        <select class="input" bind:value={scope}>
          <option value="">Every source I can view</option>
          {#if scope !== '' && !optionsByKey.has(scope)}
            <option value={scope}>{scopeLabel(scope)}</option>
          {/if}
          {#each options as option (option.key)}
            <option value={option.key}>{option.label}</option>
          {/each}
        </select>
      </label>
      <label>
        Files
        <input
          class="input mono"
          bind:value={fileGlob}
          placeholder={DEFAULT_FILE_GLOB}
          aria-describedby="alert-files-hint"
          required
        />
      </label>
      <p class="hint field-hint" id="alert-files-hint">
        A glob over each source's visible files, e.g. <code>**/*.log</code>. Compressed files and
        archives are never read.
      </p>
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
      <div class="form-actions">
        <button class="btn btn-primary" type="submit" disabled={saving}>
          {#if editingId === null}
            {saving ? 'Creating…' : 'Create alert'}
          {:else}
            {saving ? 'Saving…' : 'Save changes'}
          {/if}
        </button>
        {#if editingId !== null}
          <button class="btn btn-ghost" type="button" on:click={closeForm}>Cancel</button>
        {/if}
      </div>
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
        {@const result = testResult[alert.id]}
        <li class="card alert-row">
          <div class="alert-main">
            <div class="alert-name">{alert.name}</div>
            <div class="alert-meta">
              <span class="badge badge-accent">{scopeLabel(scopeKeyOf(alert))}</span>
              {#if alert.regex}<span class="badge">regex</span>{/if}
              <code class="query">{alert.query}</code>
            </div>
            <div class="alert-sub">
              files <code>{alert.file_glob}</code> · {alert.webhook_url}
            </div>
            <div class="alert-sub">
              last checked {formatTime(alert.last_checked_at)} · last fired
              {formatTime(alert.last_fired_at)}
            </div>
            {#if alert.failed_deliveries > 0 || alert.last_error}
              <p class="delivery-warning" role="status">⚠ {deliveryWarning(alert)}</p>
            {/if}
          </div>
          <div class="alert-actions">
            {#if result === 'sending'}
              <span class="hint">Sending…</span>
            {:else if result?.ok}
              <span class="test-ok">Sent ✓</span>
            {:else if result}
              <span class="error test-failed" title={result.error ?? ''}>
                Failed{result.error ? `: ${result.error}` : ''}
              </span>
            {/if}
            {#if canManage}
              <button class="btn btn-ghost" on:click={() => sendTest(alert)}>Test</button>
              <button class="btn btn-ghost" on:click={() => openEdit(alert)}>Edit</button>
              <label class="switch">
                <input
                  type="checkbox"
                  aria-label={`Alert ${alert.name} enabled`}
                  checked={alert.enabled}
                  on:change={() => toggleEnabled(alert)}
                />
                <span class="switch-track"></span>
              </label>
            {/if}
            <button class="btn btn-danger btn-sm" on:click={() => removeAlert(alert)}>Delete</button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 900px;
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
  h2 {
    font-size: 1rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0.35rem 0 0;
    line-height: 1.5;
  }
  .field-hint {
    font-size: 0.78rem;
    margin-top: -0.55rem;
  }
  .alert-form {
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
    padding: 1.1rem 1.5rem;
  }
  .alert-form label {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .alert-form label.check {
    flex-direction: row;
    align-items: center;
    gap: 0.45rem;
    color: var(--text);
  }
  .mono {
    font-family: var(--font-mono);
  }
  .form-actions {
    display: flex;
    gap: 0.5rem;
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
  .alert-main {
    min-width: 0;
  }
  .alert-name {
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.3rem;
  }
  .alert-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
    font-size: 0.8rem;
    margin-bottom: 0.25rem;
  }
  .alert-meta .query {
    font-family: var(--font-mono);
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .alert-sub {
    font-size: 0.75rem;
    color: var(--text-faint);
    overflow-wrap: anywhere;
  }
  .delivery-warning {
    margin: 0.4rem 0 0;
    padding: 0.3rem 0.55rem;
    border-radius: var(--radius-sm);
    background: var(--warning-soft);
    color: var(--warning);
    font-size: 0.78rem;
    overflow-wrap: anywhere;
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
  .test-ok {
    color: var(--success);
    font-size: 0.8rem;
  }
  .test-failed {
    font-size: 0.8rem;
    max-width: 16rem;
    overflow-wrap: anywhere;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
