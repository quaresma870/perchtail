<script lang="ts">
  // Bulk import/export of customers, folders, sources and rules as YAML
  // (#179). Preview first (a dry run that shows every change and every
  // problem), then apply in one go. Super admins only.
  import { api, ApiError } from '../lib/api'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { IMPORT_TEMPLATE } from '../lib/import-template'

  interface PlanItem {
    action: 'create' | 'update' | 'unchanged'
    kind: 'customer' | 'folder' | 'source'
    key: string
    changes: string[]
    needs_credential: boolean
    protocol?: string | null
  }
  interface Problem {
    where: string
    message: string
  }
  interface ImportResult {
    applied: boolean
    items: PlanItem[]
    problems: Problem[]
    // Non-blocking: e.g. a key PerchTail no longer uses, ignored.
    warnings: Problem[]
  }

  let text = ''
  let result: ImportResult | null = null
  let previewedText = ''
  let busy = false
  let error = ''
  let showUnchanged = false
  // Typed credentials for sources with none in the file or saved, by plan
  // key. SSH sources can take a private key instead of a password (#196).
  interface TypedCredential {
    username: string
    password: string
    privateKey: string
    useKey: boolean
  }
  let credentials: Record<string, TypedCredential> = {}

  $: changed = result?.items.filter((i) => i.action !== 'unchanged') ?? []
  $: unchanged = result?.items.filter((i) => i.action === 'unchanged') ?? []
  $: needingCredential = result?.items.filter((i) => i.needs_credential) ?? []
  $: stale = result !== null && text !== previewedText
  // A credential typed for a source that's otherwise unchanged is a change too.
  $: credentialOnly = needingCredential.filter(
    (i) => i.action === 'unchanged' && typedCredentials(credentials)[i.key],
  ).length
  $: applyCount = changed.length + credentialOnly

  function download(name: string, content: string) {
    const url = URL.createObjectURL(new Blob([content], { type: 'application/yaml' }))
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
  }

  async function exportConfig() {
    error = ''
    try {
      const response = await fetch('/admin/config/export', { credentials: 'include' })
      if (!response.ok) throw new ApiError(response.status, 'Export failed')
      download('perchtail-config.yaml', await response.text())
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Export failed'
    }
  }

  async function loadFile(event: Event) {
    const file = (event.target as HTMLInputElement).files?.[0]
    if (file) text = await file.text()
  }

  function typedCredentials(typed: Record<string, TypedCredential>) {
    return Object.fromEntries(
      Object.entries(typed)
        .filter(([, c]) => c.username && (c.useKey ? c.privateKey : c.password))
        .map(([key, c]) => [
          key,
          c.useKey
            ? { username: c.username, private_key: c.privateKey }
            : { username: c.username, password: c.password },
        ]),
    )
  }

  async function submit(dryRun: boolean) {
    busy = true
    error = ''
    try {
      const response = await api.post<ImportResult>('/admin/config/import', {
        yaml: text,
        dry_run: dryRun,
        credentials: typedCredentials(credentials),
      })
      result = response
      previewedText = text
      for (const item of response.items) {
        if (item.needs_credential && !credentials[item.key]) {
          credentials[item.key] = { username: '', password: '', privateKey: '', useKey: false }
        }
      }
      if (response.applied) credentials = {}
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Import failed'
    } finally {
      busy = false
    }
  }

  const actionLabel = { create: 'New', update: 'Changed', unchanged: 'Unchanged' }
</script>

<SettingsNav />

<div class="page">
  <h1>Import / export</h1>
  <p class="hint">
    Define customers, folders, sources and their rules in one YAML file. Importing matches things
    by name, so the same file can be imported again to apply edits; nothing is ever deleted.
    Credentials are never exported. See <code>docs/bulk-import.md</code>.
  </p>

  <div class="actions">
    <button class="btn btn-ghost" on:click={exportConfig}>Export current configuration</button>
    <button
      class="btn btn-ghost"
      on:click={() => download('perchtail-import-template.yaml', IMPORT_TEMPLATE)}
      >Download template</button
    >
    <button class="btn btn-ghost" on:click={() => (text = IMPORT_TEMPLATE)}>Start from template</button>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {/if}

  <div class="card editor">
    <label class="file">
      Load a file
      <input type="file" accept=".yaml,.yml,text/yaml" on:change={loadFile} />
    </label>
    <textarea
      class="input mono"
      rows="18"
      bind:value={text}
      spellcheck="false"
      autocomplete="off"
      data-gramm="false"
      aria-label="Import YAML"
      placeholder="version: 1&#10;customers:&#10;  - name: …"
    ></textarea>
    <div class="actions">
      <button class="btn btn-primary" disabled={busy || !text.trim()} on:click={() => submit(true)}>
        {busy ? 'Checking…' : 'Preview'}
      </button>
      {#if result && !result.applied && result.problems.length === 0 && !stale}
        <button class="btn btn-primary" disabled={busy || applyCount === 0} on:click={() => submit(false)}>
          Apply {applyCount} change{applyCount === 1 ? '' : 's'}
        </button>
      {/if}
    </div>
    {#if stale}
      <p class="hint">The file changed since the preview — preview again before applying.</p>
    {/if}
  </div>

  {#if result}
    {#if result.applied}
      <p class="success">Imported. {changed.length} change{changed.length === 1 ? '' : 's'} applied.</p>
    {/if}

    {#if result.problems.length > 0}
      <div class="card">
        <h2>Problems — nothing was imported</h2>
        <ul class="problems">
          {#each result.problems as problem, i (i)}
            <li><code>{problem.where}</code> {problem.message}</li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if result.warnings.length > 0}
      <div class="card">
        <h2>Warnings</h2>
        <ul class="warnings">
          {#each result.warnings as warning, i (i)}
            <li><code>{warning.where}</code> {warning.message}</li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if !result.applied && needingCredential.length > 0 && result.problems.length === 0}
      <div class="card">
        <h2>Credentials</h2>
        <p class="hint">
          These sources have no credential, in the file or saved. Type one now, or leave it blank and
          set it in the source's settings later.
        </p>
        {#each needingCredential as item (item.key)}
          {#if credentials[item.key]}
            <div class="cred-row">
              <span class="key">{item.key}</span>
              <input
                class="input"
                placeholder="username"
                bind:value={credentials[item.key].username}
                autocomplete="off"
                data-gramm="false"
              />
              {#if credentials[item.key].useKey}
                <textarea
                  class="input mono key-input"
                  rows="4"
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  aria-label={`Private key for ${item.key}`}
                  bind:value={credentials[item.key].privateKey}
                  spellcheck="false"
                  autocomplete="off"
                  data-gramm="false"
                ></textarea>
              {:else}
                <input
                  class="input"
                  type="password"
                  placeholder="password"
                  bind:value={credentials[item.key].password}
                  autocomplete="new-password"
                  data-gramm="false"
                />
              {/if}
              {#if item.protocol === 'ssh'}
                <label class="key-toggle">
                  <input type="checkbox" bind:checked={credentials[item.key].useKey} />
                  SSH key
                </label>
              {/if}
            </div>
          {/if}
        {/each}
      </div>
    {/if}

    {#if result.items.length > 0}
      <div class="card">
        <h2>{result.applied ? 'What changed' : 'What this file changes'}</h2>
        {#if changed.length === 0}
          <p class="hint">Nothing — everything already matches the file.</p>
        {:else}
          <table>
            <tbody>
              {#each changed as item (item.kind + item.key)}
                <tr>
                  <td><span class={`badge ${item.action}`}>{actionLabel[item.action]}</span></td>
                  <td class="kind">{item.kind}</td>
                  <td>
                    <div>{item.key}</div>
                    {#each item.changes as change, i (i)}
                      <div class="change">{change}</div>
                    {/each}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
        {#if unchanged.length > 0}
          <button class="btn btn-ghost small" on:click={() => (showUnchanged = !showUnchanged)}>
            {showUnchanged ? 'Hide' : 'Show'} {unchanged.length} unchanged
          </button>
          {#if showUnchanged}
            <ul class="unchanged">
              {#each unchanged as item (item.kind + item.key)}
                <li><span class="kind">{item.kind}</span> {item.key}</li>
              {/each}
            </ul>
          {/if}
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 900px;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  h2 {
    font-size: 1rem;
    margin: 0 0 0.5rem;
    color: var(--text);
  }
  .card {
    padding: 1rem 1.25rem;
  }
  .editor {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .file {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  textarea {
    width: 100%;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    white-space: pre;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
  td {
    padding: 0.35rem 0.5rem;
    border-top: 1px solid var(--border-soft);
    vertical-align: top;
    color: var(--text);
  }
  .kind {
    color: var(--text-faint);
    font-size: 0.78rem;
  }
  .change {
    color: var(--text-muted);
    font-size: 0.78rem;
    font-family: var(--font-mono);
  }
  .badge {
    font-size: 0.72rem;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    border: 1px solid var(--border);
  }
  .badge.create {
    color: var(--success, #2e7d32);
  }
  .badge.update {
    color: var(--warning, #b58105);
  }
  .problems,
  .warnings,
  .unchanged {
    margin: 0;
    padding-left: 1.1rem;
    font-size: 0.85rem;
    color: var(--text);
  }
  .problems li {
    color: var(--danger);
  }
  .warnings li {
    color: var(--warning, #b58105);
  }
  .cred-row {
    display: grid;
    grid-template-columns: 1fr 160px 220px 80px;
    gap: 0.5rem;
    align-items: center;
    margin-top: 0.4rem;
  }
  .key-input {
    font-family: var(--font-mono);
    font-size: 0.72rem;
    /* A key is one long base64 run: wrap it rather than scroll (#206). */
    min-height: 4.5rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    resize: vertical;
  }
  @media (max-width: 900px) {
    .cred-row {
      grid-template-columns: 1fr 1fr;
    }
    .cred-row .key {
      grid-column: 1 / -1;
    }
    .cred-row .key-input {
      grid-column: 1 / -1;
      order: 3;
    }
  }
  .key-toggle {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .key {
    font-size: 0.85rem;
    color: var(--text);
    overflow-wrap: anywhere;
  }
  .small {
    margin-top: 0.5rem;
    font-size: 0.8rem;
    padding: 0.25rem 0.55rem;
  }
  .error {
    color: var(--danger);
  }
  .success {
    color: var(--success, #2e7d32);
  }
  .hint {
    color: var(--text-faint);
    margin: 0;
    font-size: 0.85rem;
  }
</style>
