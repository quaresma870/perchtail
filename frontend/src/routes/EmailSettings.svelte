<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import SettingsLayout from '../lib/components/SettingsLayout.svelte'
  import { confirmAction } from '../lib/confirm'
  import type { ConnectionCheckResult, SmtpSettings } from '../lib/types'

  const DEFAULT_PORTS = { starttls: 587, tls: 465 } as const

  let settings: SmtpSettings | null = null
  let loading = true
  let saving = false
  let testing = false
  let error = ''
  let saved = false
  let testResult: ConnectionCheckResult | null = null

  let host = ''
  let port: number = DEFAULT_PORTS.starttls
  let security: 'starttls' | 'tls' = 'starttls'
  let username = ''
  let password = ''
  let removePassword = false
  let fromAddress = ''
  let fromName = 'PerchTail'
  let caPem = ''
  let enabled = false
  let recipient = ''

  function loadFormFrom(s: SmtpSettings | null) {
    host = s?.host ?? ''
    port = s?.port ?? DEFAULT_PORTS.starttls
    security = s?.security ?? 'starttls'
    username = s?.username ?? ''
    password = ''
    removePassword = false
    fromAddress = s?.from_address ?? ''
    fromName = s ? (s.from_name ?? '') : 'PerchTail'
    caPem = s?.tls_ca_pem ?? ''
    enabled = s?.enabled ?? false
  }

  async function load() {
    loading = true
    error = ''
    try {
      settings = await api.get<SmtpSettings | null>('/smtp-settings')
      loadFormFrom(settings)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load email settings'
    } finally {
      loading = false
    }
  }

  onMount(load)

  // Switching the mode moves the port along when it was the other mode's default.
  function onSecurityChange() {
    const other = security === 'tls' ? DEFAULT_PORTS.starttls : DEFAULT_PORTS.tls
    if (port === other) port = DEFAULT_PORTS[security]
  }

  // Ticking "Remove the saved password" clears whatever was typed, so the
  // field never shows a password that won't be saved; unticking leaves it empty.
  $: if (removePassword) password = ''

  async function handleSubmit() {
    error = ''
    saved = false
    saving = true
    testResult = null
    try {
      const payload: Record<string, unknown> = {
        host,
        port,
        security,
        username: username || null,
        from_address: fromAddress,
        from_name: fromName || null,
        tls_ca_pem: caPem || null,
        enabled,
      }
      // Omitted keeps the saved password; "" removes it. Removing wins.
      if (removePassword) payload.password = ''
      else if (password) payload.password = password
      settings = await api.put<SmtpSettings>('/smtp-settings', payload)
      loadFormFrom(settings)
      saved = true
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to save email settings'
    } finally {
      saving = false
    }
  }

  async function handleTest() {
    testing = true
    testResult = null
    error = ''
    try {
      testResult = await api.post<ConnectionCheckResult>(
        '/smtp-settings/test',
        recipient.trim() ? { recipient: recipient.trim() } : {},
      )
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to send the test message'
    } finally {
      testing = false
    }
  }

  async function handleDelete() {
    const ok = await confirmAction({
      title: 'Remove email settings',
      message: 'Remove the SMTP server settings, saved password included? PerchTail stops sending email.',
      confirmLabel: 'Remove',
      danger: true,
    })
    if (!ok) return
    error = ''
    try {
      await api.delete('/smtp-settings')
      settings = null
      loadFormFrom(null)
      testResult = null
      saved = false
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to remove email settings'
    }
  }
</script>

<SettingsLayout>

<div class="page">
  <h1>Email settings</h1>
  <p class="hint">
    The SMTP server PerchTail sends email through. Nothing is sent until it's configured and
    enabled. The connection always uses TLS with a verified certificate: STARTTLS (usually port
    587) or implicit TLS (usually port 465). The server can't be on this host itself or on a
    link-local address.
  </p>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}

    <form class="card" on:submit|preventDefault={handleSubmit}>
      <div class="row">
        <label class="grow">
          SMTP server
          <input class="input mono" bind:value={host} placeholder="smtp.example.com" required />
        </label>
        <label class="port">
          Port
          <input class="input mono" type="number" min="1" max="65535" bind:value={port} required />
        </label>
      </div>
      <label>
        Security
        <select class="input" bind:value={security} on:change={onSecurityChange}>
          <option value="starttls">STARTTLS</option>
          <option value="tls">Implicit TLS</option>
        </select>
      </label>
      <label>
        Username <span class="optional">(optional)</span>
        <input class="input mono" bind:value={username} autocomplete="off" />
        <span class="field-hint">Leave blank for a relay that accepts mail without signing in.</span>
      </label>
      <label>
        Password
        <input
          class="input mono"
          type="password"
          autocomplete="new-password"
          spellcheck="false"
          data-gramm="false"
          data-enable-grammarly="false"
          bind:value={password}
          disabled={!username || removePassword}
          placeholder={settings?.password_set ? 'Leave blank to keep the saved password' : ''}
        />
        {#if settings?.password_set}
          <span class="field-hint">A password is saved. It is never shown.</span>
        {/if}
      </label>
      {#if settings?.password_set && username}
        <label class="check">
          <input type="checkbox" bind:checked={removePassword} />
          Remove the saved password
        </label>
      {/if}
      <label>
        Sender address
        <input
          class="input mono"
          type="email"
          bind:value={fromAddress}
          placeholder="perchtail@example.com"
          required
        />
      </label>
      <label>
        Sender name <span class="optional">(optional)</span>
        <input class="input" bind:value={fromName} maxlength="100" />
      </label>
      <label>
        CA certificate <span class="optional">(optional)</span>
        <textarea
          class="input mono"
          rows="4"
          bind:value={caPem}
          placeholder="-----BEGIN CERTIFICATE-----"
        ></textarea>
        <span class="field-hint">
          PEM. Trusted in addition to the system's CAs, for a server whose certificate comes from
          an internal CA.
        </span>
      </label>

      <div class="switch-row">
        <label class="switch">
          <input type="checkbox" bind:checked={enabled} aria-label="Enabled" />
          <span class="switch-track"></span>
        </label>
        <span>Enabled</span>
      </div>

      <div class="actions">
        <button class="btn btn-primary" type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {#if settings}
          <button class="btn btn-danger" type="button" on:click={handleDelete}>Remove</button>
        {/if}
        {#if saved}
          <span class="saved" role="status">Saved</span>
        {/if}
      </div>
    </form>

    {#if settings}
      <form class="card" on:submit|preventDefault={handleTest}>
        <h2>Send a test message</h2>
        <label>
          Recipient
          <input
            class="input mono"
            type="email"
            bind:value={recipient}
            placeholder={$currentUser?.email ?? 'name@example.com'}
          />
          <span class="field-hint">
            Leave blank to send it to your own email. Saved settings are used, and the test is
            recorded in the audit log.
          </span>
        </label>
        <div class="actions">
          <button class="btn btn-ghost" type="submit" disabled={testing}>
            {testing ? 'Sending…' : 'Send test message'}
          </button>
        </div>
        {#if testResult}
          <p class="test-result" class:ok={testResult.ok} class:fail={!testResult.ok} role="status">
            {testResult.ok ? '✓' : '✕'}
            {testResult.detail}
          </p>
        {/if}
      </form>
    {/if}
  {/if}
</div>
</SettingsLayout>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 640px;
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
    font-size: 1rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    padding: 1.5rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 0.5rem;
  }
  .row {
    display: flex;
    gap: 0.8rem;
  }
  .grow {
    flex: 1;
  }
  .port {
    width: 7rem;
  }
  .mono {
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }
  textarea {
    resize: vertical;
  }
  .switch-row {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 0.7rem;
    color: var(--text);
    font-size: 0.88rem;
  }
  .actions {
    display: flex;
    gap: 0.6rem;
    align-items: center;
  }
  .saved {
    font-size: 0.85rem;
    color: var(--success);
  }
  .test-result {
    margin: 0;
    font-size: 0.85rem;
    padding: 0.5rem 0.7rem;
    border-radius: var(--radius-sm);
  }
  .test-result.ok {
    background: var(--success-soft);
    color: var(--success);
  }
  .test-result.fail {
    background: var(--danger-soft);
    color: var(--danger);
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
  .optional {
    font-weight: 400;
    color: var(--text-faint);
  }
  .field-hint {
    font-size: 0.78rem;
    color: var(--text-faint);
    line-height: 1.4;
  }
</style>
