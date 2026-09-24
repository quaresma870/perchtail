<script lang="ts">
  import QRCode from 'qrcode'
  import { api, ApiError } from '../lib/api'
  import { currentUser, refreshCurrentUser } from '../lib/auth'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type { MfaBackupCodesResponse, MfaEnrollResponse } from '../lib/types'

  // Four states this page can be in, driven by $currentUser.mfa_enabled plus
  // local "mid enrollment" / "just confirmed" flags:
  //   1. idle, MFA off       -> "Enable" button
  //   2. enrolling           -> QR code + secret + code input
  //   3. just confirmed      -> one-time backup-code display
  //   4. idle, MFA on        -> disable / regenerate actions
  let enrollment: MfaEnrollResponse | null = null
  let qrDataUrl = ''
  let confirmCode = ''
  let backupCodes: string[] | null = null
  let enrollPassword = ''
  let disablePassword = ''
  let regeneratePassword = ''
  let showEnrollForm = false
  let showRegenerateForm = false
  let showDisableForm = false
  let error = ''
  let busy = false

  async function startEnroll() {
    error = ''
    busy = true
    try {
      // The API requires re-confirming the current password to start
      // enrollment -- a session cookie alone shouldn't be enough to enroll
      // a new authenticator device (see backend/app/api/auth.py's
      // MfaPasswordConfirmRequest).
      enrollment = await api.post<MfaEnrollResponse>('/auth/mfa/enroll', {
        password: enrollPassword,
      })
      enrollPassword = ''
      showEnrollForm = false
      qrDataUrl = await QRCode.toDataURL(enrollment.otpauth_uri)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to start enrollment'
    } finally {
      busy = false
    }
  }

  function cancelEnroll() {
    enrollment = null
    qrDataUrl = ''
    confirmCode = ''
    error = ''
  }

  async function confirmEnroll() {
    error = ''
    busy = true
    try {
      const result = await api.post<MfaBackupCodesResponse>('/auth/mfa/confirm', {
        code: confirmCode,
      })
      backupCodes = result.backup_codes
      enrollment = null
      qrDataUrl = ''
      confirmCode = ''
      await refreshCurrentUser()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to confirm the code'
    } finally {
      busy = false
    }
  }

  function dismissBackupCodes() {
    backupCodes = null
  }

  async function disable() {
    error = ''
    busy = true
    try {
      await api.post('/auth/mfa/disable', { password: disablePassword })
      disablePassword = ''
      showDisableForm = false
      await refreshCurrentUser()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to disable MFA'
    } finally {
      busy = false
    }
  }

  async function regenerateBackupCodes() {
    error = ''
    busy = true
    try {
      const result = await api.post<MfaBackupCodesResponse>('/auth/mfa/backup-codes/regenerate', {
        password: regeneratePassword,
      })
      backupCodes = result.backup_codes
      regeneratePassword = ''
      showRegenerateForm = false
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to regenerate backup codes'
    } finally {
      busy = false
    }
  }

  function copyBackupCodes() {
    navigator.clipboard?.writeText(backupCodes?.join('\n') ?? '').catch(() => {})
  }
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Security</h1>
    <p class="hint">
      Optional two-factor authentication for your own local account, using an authenticator app
      (TOTP).
    </p>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if backupCodes}
    <div class="card backup-codes">
      <h2>Save your backup codes</h2>
      <p class="hint">
        Each code can be used once, in place of your authenticator app, if you lose access to it.
        This is the only time they're shown — store them somewhere safe.
      </p>
      <ul class="code-grid">
        {#each backupCodes as code (code)}
          <li>{code}</li>
        {/each}
      </ul>
      <div class="actions">
        <button class="btn btn-ghost" on:click={copyBackupCodes}>Copy codes</button>
        <button class="btn btn-primary" on:click={dismissBackupCodes}>Done</button>
      </div>
    </div>
  {:else if enrollment}
    <div class="card">
      <h2>Scan this code</h2>
      <p class="hint">
        Scan with your authenticator app (Google Authenticator, 1Password, Authy, …), then enter
        the 6-digit code it shows to confirm.
      </p>
      {#if qrDataUrl}
        <img class="qr" src={qrDataUrl} alt="QR code for authenticator app enrollment" />
      {/if}
      <p class="secret">
        Can't scan it? Enter this key manually: <code>{enrollment.secret}</code>
      </p>
      <label>
        Confirmation code
        <input
          class="input"
          type="text"
          bind:value={confirmCode}
          inputmode="numeric"
          autocomplete="one-time-code"
        />
      </label>
      <div class="actions">
        <button class="btn btn-ghost" on:click={cancelEnroll} disabled={busy}>Cancel</button>
        <button class="btn btn-primary" on:click={confirmEnroll} disabled={busy || !confirmCode}>
          {busy ? 'Confirming…' : 'Confirm'}
        </button>
      </div>
    </div>
  {:else if $currentUser?.mfa_enabled}
    <div class="card">
      <h2>Two-factor authentication is on</h2>
      <p class="hint">Your account requires an authenticator code (or a backup code) to sign in.</p>

      {#if showRegenerateForm}
        <label>
          Current password
          <input class="input" type="password" bind:value={regeneratePassword} />
        </label>
        <div class="actions">
          <button
            class="btn btn-ghost"
            on:click={() => {
              showRegenerateForm = false
              regeneratePassword = ''
            }}>Cancel</button
          >
          <button
            class="btn btn-primary"
            on:click={regenerateBackupCodes}
            disabled={busy || !regeneratePassword}
          >
            {busy ? 'Regenerating…' : 'Regenerate backup codes'}
          </button>
        </div>
      {:else if showDisableForm}
        <label>
          Current password
          <input class="input" type="password" bind:value={disablePassword} />
        </label>
        <div class="actions">
          <button
            class="btn btn-ghost"
            on:click={() => {
              showDisableForm = false
              disablePassword = ''
            }}>Cancel</button
          >
          <button class="btn btn-danger" on:click={disable} disabled={busy || !disablePassword}>
            {busy ? 'Disabling…' : 'Disable MFA'}
          </button>
        </div>
      {:else}
        <div class="actions">
          <button class="btn btn-ghost" on:click={() => (showRegenerateForm = true)}>
            Regenerate backup codes
          </button>
          <button class="btn btn-ghost danger" on:click={() => (showDisableForm = true)}>
            Disable MFA
          </button>
        </div>
      {/if}
    </div>
  {:else}
    <div class="card">
      <h2>Two-factor authentication is off</h2>
      <p class="hint">
        Add an extra layer of protection to your account with a 6-digit code from an
        authenticator app.
      </p>
      {#if showEnrollForm}
        <label>
          Current password
          <input class="input" type="password" bind:value={enrollPassword} />
        </label>
        <div class="actions">
          <button
            class="btn btn-ghost"
            on:click={() => {
              showEnrollForm = false
              enrollPassword = ''
            }}>Cancel</button
          >
          <button class="btn btn-primary" on:click={startEnroll} disabled={busy || !enrollPassword}>
            {busy ? 'Starting…' : 'Continue'}
          </button>
        </div>
      {:else}
        <button class="btn btn-primary" on:click={() => (showEnrollForm = true)}>
          Enable two-factor authentication
        </button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 640px;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  h2 {
    font-size: 1.05rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0.35rem 0 0;
    line-height: 1.5;
  }
  .card {
    padding: 1.25rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .qr {
    width: 200px;
    height: 200px;
    align-self: flex-start;
    background: #fff;
    padding: 0.5rem;
    border-radius: 6px;
  }
  .secret {
    font-size: 0.82rem;
    color: var(--text-faint);
    margin: 0;
  }
  .secret code {
    color: var(--text);
    word-break: break-all;
  }
  .actions {
    display: flex;
    gap: 0.6rem;
  }
  .btn-ghost.danger {
    color: var(--danger);
  }
  .btn-danger {
    background: var(--danger);
    color: #fff;
    border: none;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
  .backup-codes .code-grid {
    list-style: none;
    margin: 0;
    padding: 0.9rem 1.1rem;
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.5rem 1.5rem;
    background: var(--bg);
    border-radius: 6px;
    font-family: monospace;
    font-size: 0.95rem;
    color: var(--text);
  }
</style>
