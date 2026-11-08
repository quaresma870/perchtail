<script lang="ts">
  import { onDestroy } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser, hasCapability } from '../lib/auth'
  import RuleEditor from '../lib/components/RuleEditor.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { justCreatedSourceId } from '../lib/source-created'
  import { ago } from '../lib/source-status'
  import type { AgentTokenResult, ConnectionCheckResult, Source } from '../lib/types'

  /** Read-only overview of a source -- where creating one lands (#157),
   * instead of back on its edit form. Shows what the source is, whether it
   * has rules (a source with none shows nothing), its rules, and the agent
   * token for agent sources; editing is one click away. */
  export let params: { id?: string } = {}

  // Reactive, not read once: going from one source's page to another's
  // (a link, or back/forward) reuses this component with new params (#192).
  $: sourceId = Number(params.id)

  let source: Source | null = null
  let loading = true
  let error = ''
  let created = false
  let ruleCount = 0

  let checking = false
  let check: ConnectionCheckResult | null = null

  let agentToken = ''
  let generatingToken = false
  let tokenError = ''

  // Go's GOOS values as people write them (#206).
  const OS_NAMES: Record<string, string> = {
    linux: 'Linux',
    windows: 'Windows',
    darwin: 'macOS',
    freebsd: 'FreeBSD',
    openbsd: 'OpenBSD',
    netbsd: 'NetBSD',
  }
  function osName(goos: string): string {
    return OS_NAMES[goos] ?? goos
  }

  $: canManage = hasCapability($currentUser, 'create_source') && !source?.is_system

  let justCreated: number | null = null
  const unsubscribe = justCreatedSourceId.subscribe((id) => {
    justCreated = id
  })
  $: created = justCreated === sourceId

  $: load(sourceId)

  async function load(id: number) {
    loading = true
    error = ''
    source = null
    check = null
    agentToken = ''
    tokenError = ''
    try {
      const loaded = await api.get<Source>(`/sources/${id}`)
      if (id !== sourceId) return // a newer load took over
      source = loaded
      ruleCount = loaded.rule_count
    } catch (err) {
      if (id !== sourceId) return
      error = err instanceof ApiError ? err.detail : 'Failed to load source'
    } finally {
      if (id === sourceId) loading = false
    }
  }

  // "Index now" for the file-name index (#304).
  let indexing = false
  let indexNote = ''
  $: canIndexNow = source?.my_capabilities.includes('run_now') ?? false

  async function indexNow() {
    if (!source) return
    indexing = true
    indexNote = ''
    try {
      const result = await api.post<{ started: boolean }>(`/sources/${source.id}/name-index`)
      indexNote = result.started
        ? 'Listing the files in the background. Reload this page in a moment to see the result.'
        : 'Already listing this source.'
    } catch (err) {
      indexNote = err instanceof ApiError ? err.detail : 'Failed to start the listing'
    } finally {
      indexing = false
    }
  }

  async function testConnection() {
    checking = true
    check = null
    try {
      check = await api.post<ConnectionCheckResult>(`/sources/${sourceId}/check`)
    } catch (err) {
      check = { ok: false, detail: err instanceof ApiError ? err.detail : 'Check failed' }
    } finally {
      checking = false
    }
  }

  async function generateAgentToken() {
    tokenError = ''
    generatingToken = true
    try {
      agentToken = (await api.post<AgentTokenResult>(`/sources/${sourceId}/agent-token`)).token
      if (source) source = { ...source, has_agent_token: true }
    } catch (err) {
      tokenError = err instanceof ApiError ? err.detail : 'Failed to generate token'
    } finally {
      generatingToken = false
    }
  }

  onDestroy(() => {
    unsubscribe()
    justCreatedSourceId.set(null)
  })
</script>

<SettingsNav />

<div class="page">
  {#if loading}
    <p class="hint">Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if source}
    {#if created}
      <div class="notice success" role="status">Source created.</div>
    {/if}

    <div class="header">
      <div>
        <h1>{source.name}</h1>
        <div class="badges">
          <span class="badge">{source.protocol}</span>
          {#if source.is_system}<span class="badge badge-accent">system</span>{/if}
          {#if !source.enabled}<span class="badge">disabled</span>{/if}
        </div>
      </div>
      <div class="header-actions">
        <button
          class="btn btn-primary"
          disabled={!source.enabled || ruleCount === 0}
          title={!source.enabled
            ? 'This source is disabled: turn it on in its settings to browse it'
            : ruleCount === 0
              ? 'Add an include rule first: a source with no rules shows nothing'
              : undefined}
          on:click={() => push(`/viewer/${sourceId}`)}>Browse</button
        >
        <button
          class="btn btn-ghost"
          disabled={checking || !source.enabled}
          title={source.enabled ? undefined : 'This source is disabled: turn it on in its settings to test it'}
          on:click={testConnection}
        >
          {checking ? 'Testing…' : 'Test connection'}
        </button>
        {#if canManage}
          <button class="btn btn-ghost" on:click={() => push(`/settings/sources/${sourceId}/edit`)}>Edit</button>
        {/if}
        <button class="btn btn-ghost" on:click={() => push('/settings/sources')}>← back to sources</button>
      </div>
    </div>

    {#if check}
      <div class="notice" class:success={check.ok} class:failure={!check.ok} role="status">
        {check.ok ? 'Connected.' : check.detail}
      </div>
    {/if}

    <dl class="details card">
      <dt>Customer</dt>
      <dd>{source.customer_name ?? '—'}</dd>
      <dt>Folder</dt>
      <dd>{source.folder_path.length ? source.folder_path.map((f) => f.name).join(' / ') : '—'}</dd>
      {#if source.protocol !== 'agent'}
        <dt>Host</dt>
        <dd class="mono">
          {#if source.winrm_effective_scheme}{source.winrm_effective_scheme}://{/if}{source.host}{source.port
            ? `:${source.port}`
            : ''}
          {#if source.tls_ca_pem}<span class="badge badge-muted">own CA</span>{/if}
          {#if source.winrm_effective_scheme === 'http'}
            <p class="http-warning">
              Plain HTTP: NTLM still encrypts the WinRM messages, but the server isn't
              authenticated. Prefer HTTPS.
            </p>
          {/if}
        </dd>
      {/if}
      {#if source.roots.length > 0}
        <dt>Folders</dt>
        <dd>
          <ul class="roots">
            {#each source.roots as root (root.label)}
              <li><strong>{root.label}</strong> <span class="mono">{root.path}</span></li>
            {/each}
          </ul>
        </dd>
      {:else}
        <dt>Base path</dt>
        <dd class="mono">{source.base_path || '/'}</dd>
      {/if}
      {#if source.protocol === 'agent'}
        <dt>Agent</dt>
        <dd>{source.agent_connected ? 'connected' : 'not connected'}</dd>
        <dt>Agent OS</dt>
        <dd>
          {#if source.agent_os === 'windows'}
            Windows — rules ignore case
          {:else if source.agent_os}
            {osName(source.agent_os)} — rules are case-sensitive
          {:else}
            not reported yet (agent older than 0.5.1, or never connected) — rules are case-sensitive
          {/if}
        </dd>
        <dt>Agent version</dt>
        <dd>
          {#if source.agent_version}
            {source.agent_version}
            {#if source.agent_outdated}
              <span class="agent-outdated">— older than this server; upgrade the agent</span>
            {/if}
          {:else}
            not reported yet (agent older than 0.6, or never connected)
          {/if}
        </dd>
      {/if}
      <dt>File names</dt>
      <dd>
        {#if !source.name_index_enabled}
          not indexed (turned off for this source)
        {:else if source.name_indexed_at}
          {(source.name_index_files ?? 0).toLocaleString()}
          {source.name_index_files === 1 ? 'file' : 'files'}, listed {ago(source.name_indexed_at, new Date())}
          {#if source.name_index_message}
            <span class="index-note">— {source.name_index_message}</span>
          {/if}
        {:else}
          not listed yet
        {/if}
        {#if source.name_index_enabled && canIndexNow}
          <button class="btn btn-ghost btn-sm index-now" disabled={indexing} on:click={indexNow}>Index now</button>
        {/if}
        {#if indexNote}
          <p class="hint index-note" role="status">{indexNote}</p>
        {/if}
      </dd>
    </dl>

    {#if ruleCount === 0}
      <div class="notice warning" role="status">
        <strong>No rules yet — nothing is visible until you add an include rule.</strong>
        Start with something like <code>**/*.log</code> below. Rules are evaluated in order and the
        last match wins.
      </div>
    {/if}

    {#if source.protocol === 'agent' && canManage}
      <fieldset class="card">
        <legend>Agent enrollment</legend>
        <p class="hint">
          The agent connects to PerchTail with this token. It's shown once; regenerating it
          disconnects the agent until its configuration has the new one.
        </p>
        {#if tokenError}<p class="error">{tokenError}</p>{/if}
        <button class="btn btn-ghost" on:click={generateAgentToken} disabled={generatingToken}>
          {generatingToken ? 'Generating…' : source.has_agent_token ? 'Regenerate token' : 'Generate token'}
        </button>
        {#if agentToken}
          <code class="token-box">{agentToken}</code>
        {/if}
      </fieldset>
    {/if}

    {#key sourceId}
      <RuleEditor
        {sourceId}
        readOnly={source.is_system}
        on:change={(e) => (ruleCount = e.detail.count)}
      />
    {/key}
  {/if}
</div>

<style>
  .agent-outdated {
    color: var(--warning);
  }
  .http-warning {
    margin: 0.3rem 0 0;
    font-family: var(--font-sans, inherit);
    font-size: 0.8rem;
    color: var(--warning, #b58105);
  }
  .roots {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .page {
    padding: 1.75rem 2rem;
    max-width: 760px;
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    flex-wrap: wrap;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0 0 0.35rem;
    color: var(--text);
  }
  .badges {
    display: flex;
    gap: 0.35rem;
    flex-wrap: wrap;
  }
  .header-actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .details {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.45rem 1.25rem;
    margin: 0;
    padding: 1rem 1.25rem;
  }
  dt {
    color: var(--text-faint);
    font-size: 0.82rem;
  }
  dd {
    margin: 0;
    color: var(--text);
    font-size: 0.88rem;
    word-break: break-all;
  }
  .notice {
    padding: 0.6rem 0.9rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    font-size: 0.86rem;
  }
  .notice.success {
    border-color: var(--success, #34d399);
  }
  .notice.failure {
    border-color: var(--danger, #f87171);
  }
  .notice.warning {
    border-color: var(--warning, #fbbf24);
  }
  fieldset {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.9rem;
  }
  legend {
    font-size: 0.8rem;
    color: var(--text-muted);
    padding: 0 0.3rem;
  }
  .token-box {
    display: block;
    padding: 0.5rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    word-break: break-all;
    font-size: 0.8rem;
  }
  .hint {
    color: var(--text-faint);
    font-size: 0.82rem;
    margin: 0;
  }
  .error {
    color: var(--danger);
  }
  .index-now {
    margin-left: 0.6rem;
  }
  .index-note {
    color: var(--text-muted);
  }
</style>
