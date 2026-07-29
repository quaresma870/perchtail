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
  <form on:submit|preventDefault={handleSubmit}>
    <h1>PerchTail</h1>
    <label>
      Username
      <input type="text" bind:value={username} autocomplete="username" required />
    </label>
    <label>
      Password
      <input type="password" bind:value={password} autocomplete="current-password" required />
    </label>
    {#if error}
      <p class="error">{error}</p>
    {/if}
    <button type="submit" disabled={submitting}>{submitting ? 'Signing in…' : 'Sign in'}</button>
  </form>
</div>

<style>
  .login-page {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    background: #1f2430;
  }
  form {
    background: #fff;
    padding: 2rem 2.25rem;
    border-radius: 8px;
    width: 320px;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
  }
  h1 {
    margin: 0 0 0.5rem;
    font-size: 1.4rem;
    text-align: center;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    font-size: 0.85rem;
    color: #444;
  }
  input {
    padding: 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
  }
  button {
    margin-top: 0.5rem;
    padding: 0.6rem;
    border: none;
    border-radius: 4px;
    background: #2f6fed;
    color: #fff;
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.6;
  }
  .error {
    color: #c0392b;
    font-size: 0.85rem;
    margin: 0;
  }
</style>
