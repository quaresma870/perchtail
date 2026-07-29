<script lang="ts">
  import { push } from 'svelte-spa-router'
  import { login } from '../lib/auth'
  import { ApiError } from '../lib/api'

  let username = ''
  let password = ''
  let error = ''
  let submitting = false

  async function handleSubmit() {
    error = ''
    submitting = true
    try {
      await login(username, password)
      push('/viewer')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Login failed'
    } finally {
      submitting = false
    }
  }
</script>

<div class="login-page">
  <form class="card" on:submit|preventDefault={handleSubmit}>
    <div class="brand">
      <img src="/favicon.svg" alt="" width="40" height="40" />
      <h1>PerchTail</h1>
    </div>
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
</style>
