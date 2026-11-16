<script lang="ts">
  // Password recovery by email (#244), step one: ask for a link. The answer
  // is the same whether or not an account matched, so the page never says.
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import type { LoginOptions } from '../lib/types'

  let identifier = ''
  let error = ''
  let submitting = false
  let sent = false
  let available: boolean | null = null

  onMount(async () => {
    const options = await api
      .get<LoginOptions>('/auth/login-options')
      .catch(() => ({ password_reset: false }))
    available = options.password_reset
  })

  async function handleSubmit() {
    error = ''
    submitting = true
    try {
      await api.post('/auth/password-reset/request', { identifier })
      sent = true
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        available = false
      } else {
        error = err instanceof ApiError ? err.detail : 'Could not send the request'
      }
    } finally {
      submitting = false
    }
  }
</script>

<div class="page">
  <div class="card panel">
    <h1>Reset your password</h1>
    {#if available === null}
      <p class="hint">Loading…</p>
    {:else if !available}
      <p class="hint">
        Password recovery by email isn't available. Ask your PerchTail administrator to reset your
        password.
      </p>
    {:else if sent}
      <p class="ok" role="status">
        If an account matches, an email with a link to reset its password is on its way. The link
        works for a limited time and only once.
      </p>
      <p class="hint">
        Accounts that sign in through SSO and administrator accounts can't reset their password
        this way.
      </p>
    {:else}
      <form on:submit|preventDefault={handleSubmit}>
        <p class="hint">
          Enter your username or the email address of your account. If it can use password
          recovery, we'll email it a link to choose a new password.
        </p>
        <label>
          Username or email
          <input
            class="input"
            type="text"
            bind:value={identifier}
            autocomplete="username"
            maxlength="254"
            required
          />
        </label>
        {#if error}
          <p class="error">{error}</p>
        {/if}
        <button class="btn btn-primary" type="submit" disabled={submitting}>
          {submitting ? 'Sending…' : 'Send the link'}
        </button>
      </form>
    {/if}
    <a class="back" href="#/login">Back to sign in</a>
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
  .ok {
    color: var(--success);
    font-size: 0.85rem;
    margin: 0;
    line-height: 1.45;
  }
  .back {
    font-size: 0.85rem;
    color: var(--accent-hover);
  }
</style>
