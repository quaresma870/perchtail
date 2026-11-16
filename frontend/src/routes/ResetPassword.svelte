<script lang="ts">
  // Password recovery by email (#244), step two: the link from the email
  // opens this page with the token in the URL fragment. The token is taken
  // out of the address bar and history at once, checked, and used once to
  // set the new password. No session is opened: the user signs in again.
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { loginNotice } from '../lib/login-notice'
  import PasswordRules from '../lib/components/PasswordRules.svelte'
  import type { PasswordPolicy, PasswordResetCheck } from '../lib/types'

  export let params: { token?: string } = {}

  let token = ''
  let state: 'checking' | 'ready' | 'invalid' = 'checking'
  let policy: PasswordPolicy | null = null
  let newPassword = ''
  let error = ''
  let submitting = false

  onMount(async () => {
    token = params.token ?? ''
    // Out of the address bar and the history entry, before anything else.
    if (token) history.replaceState(history.state, '', '#/reset-password')
    if (!token) {
      state = 'invalid'
      return
    }
    try {
      const result = await api.post<PasswordResetCheck>('/auth/password-reset/check', { token })
      policy = result.policy
      state = result.valid ? 'ready' : 'invalid'
    } catch (err) {
      state = 'invalid'
      if (err instanceof ApiError && err.status === 429) error = err.detail
    }
  })

  async function handleSubmit() {
    error = ''
    submitting = true
    try {
      await api.post('/auth/password-reset/complete', { token, new_password: newPassword })
      token = ''
      newPassword = ''
      loginNotice.set('Your password was changed. Sign in with the new one.')
      push('/login')
    } catch (err) {
      if (err instanceof ApiError && (err.errorCode === 'reset_link_invalid' || err.status === 404)) {
        token = ''
        state = 'invalid'
      } else {
        error = err instanceof ApiError ? err.detail : 'Could not change your password'
      }
    } finally {
      submitting = false
    }
  }
</script>

<div class="page">
  <div class="card panel">
    <h1>Choose a new password</h1>
    {#if state === 'checking'}
      <p class="hint">Loading…</p>
    {:else if state === 'invalid'}
      <p class="hint">This link is invalid or has expired. Ask for a new one.</p>
      {#if error}
        <p class="error">{error}</p>
      {/if}
      <a class="link" href="#/forgot-password">Ask for a new link</a>
    {:else}
      <form on:submit|preventDefault={handleSubmit}>
        <p class="hint">
          Setting it signs you out everywhere. You'll sign in again with the new password, and
          with your authentication code if you use two-factor authentication.
        </p>
        <label>
          New password
          <input
            class="input"
            type="password"
            autocomplete="new-password"
            bind:value={newPassword}
            minlength={policy?.min_length ?? 8}
            maxlength={policy?.max_length ?? 1024}
            required
          />
        </label>
        {#if policy}
          <PasswordRules {policy} />
        {/if}
        {#if error}
          <p class="error">{error}</p>
        {/if}
        <button class="btn btn-primary" type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : 'Set the password'}
        </button>
      </form>
    {/if}
    <a class="link" href="#/login">Back to sign in</a>
  </div>
</div>

<style>
  .page {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    background: var(--bg);
    background-image: radial-gradient(circle at 50% 0%, #1b1f30 0%, var(--bg) 60%);
  }
  .panel {
    padding: 2rem 2.25rem;
    width: 340px;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  h1 {
    margin: 0;
    font-size: 1.2rem;
    color: var(--text);
  }
  .hint {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-muted);
    line-height: 1.45;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  button {
    padding: 0.65rem;
  }
  .error {
    color: var(--danger);
    font-size: 0.85rem;
    margin: 0;
  }
  .link {
    font-size: 0.85rem;
    color: var(--accent-hover);
  }
</style>
