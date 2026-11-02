<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { login } from '../lib/auth'
  import { api, ApiError } from '../lib/api'
  import type { SSOStatus } from '../lib/types'

  let username = ''
  let password = ''
  let mfaCode = ''
  let error = ''
  let submitting = false
  let sso: SSOStatus = { enabled: false, name: null }
  // Set once the password step succeeds but the account needs a second
  // factor -- switches the form to asking for the TOTP/backup code instead
  // of restarting from username/password (see lib/auth.ts's login()).
  let awaitingMfaCode = false

  onMount(async () => {
    if (window.location.hash.includes('sso_error=1')) {
      error = 'SSO sign-in failed — please try again or use your local account.'
    }
    sso = await api.get<SSOStatus>('/auth/sso/status').catch(() => ({ enabled: false, name: null }))
  })

  async function handleSubmit() {
    error = ''
    submitting = true
    try {
      await login(username, password, awaitingMfaCode ? mfaCode : undefined)
      push('/viewer')
    } catch (err) {
      if (err instanceof ApiError && err.errorCode === 'mfa_required') {
        awaitingMfaCode = true
      } else if (err instanceof ApiError && err.errorCode === 'mfa_invalid_code') {
        awaitingMfaCode = true
        mfaCode = ''
        error = err.detail
      } else {
        error = err instanceof ApiError ? err.detail : 'Login failed'
      }
    } finally {
      submitting = false
    }
  }

  function backToPassword() {
    awaitingMfaCode = false
    mfaCode = ''
    error = ''
  }

  function autofocus(node: HTMLElement) {
    node.focus()
  }
</script>

<div class="login-page">
  <form class="card" on:submit|preventDefault={handleSubmit}>
    <div class="brand">
      <img src="/favicon.svg" alt="" width="40" height="40" />
      <h1>PerchTail</h1>
    </div>
    {#if awaitingMfaCode}
      <p class="hint">Enter the 6-digit code from your authenticator app, or a backup code.</p>
      <label>
        Authentication code
        <input
          class="input"
          type="text"
          bind:value={mfaCode}
          autocomplete="one-time-code"
          inputmode="numeric"
          use:autofocus
          required
        />
      </label>
      {#if error}
        <p class="error">{error}</p>
      {/if}
      <button class="btn btn-primary" type="submit" disabled={submitting}>
        {submitting ? 'Verifying…' : 'Verify'}
      </button>
      <button class="btn btn-ghost" type="button" on:click={backToPassword}>
        Back
      </button>
    {:else}
      <label>
        Username
        <input class="input" type="text" bind:value={username} autocomplete="username" required />
      </label>
      <label>
        Password
        <input
          class="input"
          type="password"
          bind:value={password}
          autocomplete="current-password"
          required
        />
      </label>
      {#if error}
        <p class="error">{error}</p>
      {/if}
      <button class="btn btn-primary" type="submit" disabled={submitting}>
        {submitting ? 'Signing in…' : 'Sign in'}
      </button>

      {#if sso.enabled}
        <div class="divider"><span>or</span></div>
        <a class="btn btn-ghost sso-btn" href="/auth/sso/login">
          Sign in with {sso.name}
        </a>
      {/if}
    {/if}
  </form>
</div>

<style>
  .login-page {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    background: var(--bg);
    background-image: radial-gradient(circle at 50% 0%, #1b1f30 0%, var(--bg) 60%);
  }
  form {
    padding: 2.25rem 2.25rem;
    width: 340px;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .brand {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.25rem;
  }
  h1 {
    margin: 0;
    font-size: 1.3rem;
    color: var(--text);
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  input {
    width: 100%;
  }
  button {
    margin-top: 0.4rem;
    padding: 0.65rem;
  }
  .error {
    color: var(--danger);
    font-size: 0.85rem;
    margin: 0;
  }
  .hint {
    color: var(--text-faint);
    font-size: 0.8rem;
    margin: -0.4rem 0 0;
    line-height: 1.4;
  }
  .divider {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    color: var(--text-faint);
    font-size: 0.78rem;
    margin: 0.2rem 0;
  }
  .divider::before,
  .divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border-soft);
  }
  .sso-btn {
    text-align: center;
    text-decoration: none;
    padding: 0.65rem;
  }
</style>
