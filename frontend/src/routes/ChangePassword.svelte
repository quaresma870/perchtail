<script lang="ts">
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import type { CurrentUser } from '../lib/types'

  let currentPassword = ''
  let newPassword = ''
  let error = ''
  let submitting = false

  async function handleSubmit() {
    error = ''
    submitting = true
    try {
      const user = await api.post<CurrentUser>('/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      })
      currentUser.set(user)
      push('/viewer')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Could not change password'
    } finally {
      submitting = false
    }
  }
</script>

<div class="page">
  <form class="card" on:submit|preventDefault={handleSubmit}>
    <h1>Set a new password</h1>
    <p class="hint">Your account was created by an admin — pick your own password to continue.</p>
    <label>
      Current (temporary) password
      <input class="input" type="password" bind:value={currentPassword} required />
    </label>
    <label>
      New password
      <input class="input" type="password" bind:value={newPassword} minlength="8" required />
    </label>
    {#if error}
      <p class="error">{error}</p>
    {/if}
    <button class="btn btn-primary" type="submit" disabled={submitting}>
      {submitting ? 'Saving…' : 'Save'}
    </button>
  </form>
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
  form {
    padding: 2rem 2.25rem;
    width: 340px;
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
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
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
