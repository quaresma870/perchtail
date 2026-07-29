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
  <form on:submit|preventDefault={handleSubmit}>
    <h1>Set a new password</h1>
    <p class="hint">Your account was created by an admin — pick your own password to continue.</p>
    <label>
      Current (temporary) password
      <input type="password" bind:value={currentPassword} required />
    </label>
    <label>
      New password
      <input type="password" bind:value={newPassword} minlength="8" required />
    </label>
    {#if error}
      <p class="error">{error}</p>
    {/if}
    <button type="submit" disabled={submitting}>{submitting ? 'Saving…' : 'Save'}</button>
  </form>
</div>

<style>
  .page {
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
    width: 340px;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
  }
  h1 {
    margin: 0;
    font-size: 1.2rem;
  }
  .hint {
    margin: 0;
    font-size: 0.85rem;
    color: #666;
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
  .error {
    color: #c0392b;
    font-size: 0.85rem;
    margin: 0;
  }
</style>
