<script lang="ts">
  // My profile (#243): name and email, password, two-factor authentication
  // and sessions, for the signed-in user. An SSO account's name and email
  // come from the identity provider, and it has no password or MFA here.
  import { api, ApiError } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import MfaPanel from '../lib/components/MfaPanel.svelte'
  import PasswordRules from '../lib/components/PasswordRules.svelte'
  import SessionsPanel from '../lib/components/SessionsPanel.svelte'
  import type { CurrentUser, PasswordPolicy } from '../lib/types'

  $: isLocal = $currentUser?.auth_provider === 'local'

  let displayName = $currentUser?.display_name ?? ''
  let email = $currentUser?.email ?? ''
  let profileSaving = false
  let profileError = ''
  let profileSaved = false

  $: profileDirty =
    displayName.trim() !== ($currentUser?.display_name ?? '') ||
    email.trim().toLowerCase() !== ($currentUser?.email ?? '')

  async function saveProfile() {
    profileSaving = true
    profileError = ''
    profileSaved = false
    try {
      const user = await api.patch<CurrentUser>('/auth/me', {
        display_name: displayName,
        email,
      })
      currentUser.set(user)
      displayName = user.display_name ?? ''
      email = user.email ?? ''
      profileSaved = true
    } catch (err) {
      profileError = err instanceof ApiError ? err.detail : 'Could not save your profile'
    } finally {
      profileSaving = false
    }
  }

  let currentPassword = ''
  let newPassword = ''
  let passwordSaving = false
  let passwordError = ''
  let passwordSaved = false
  let policy: PasswordPolicy | null = null

  async function changePassword() {
    passwordSaving = true
    passwordError = ''
    passwordSaved = false
    try {
      const user = await api.post<CurrentUser>('/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      })
      currentUser.set(user)
      currentPassword = ''
      newPassword = ''
      passwordSaved = true
    } catch (err) {
      passwordError = err instanceof ApiError ? err.detail : 'Could not change your password'
    } finally {
      passwordSaving = false
    }
  }
</script>

<div class="page">
  <h1>My profile</h1>

  <section class="card section">
    <h2>Name and email</h2>
    {#if isLocal}
      <form class="fields" on:submit|preventDefault={saveProfile}>
        <label>
          Username
          <input class="input" value={$currentUser?.username ?? ''} disabled />
        </label>
        <label>
          Display name
          <input class="input" bind:value={displayName} maxlength="100" autocomplete="name" />
        </label>
        <label>
          Email
          <input class="input" type="email" bind:value={email} autocomplete="email" />
        </label>
        {#if profileError}
          <p class="error">{profileError}</p>
        {:else if profileSaved && !profileDirty}
          <p class="ok" role="status">Saved.</p>
        {/if}
        <button class="btn btn-primary" type="submit" disabled={profileSaving || !profileDirty}>
          {profileSaving ? 'Saving…' : 'Save'}
        </button>
      </form>
    {:else}
      <p class="hint">
        These come from your identity provider and are updated each time you sign in.
      </p>
      <dl class="facts">
        <dt>Username</dt>
        <dd>{$currentUser?.username}</dd>
        <dt>Name</dt>
        <dd>{$currentUser?.display_name ?? '—'}</dd>
        <dt>Email</dt>
        <dd>{$currentUser?.email ?? '—'}</dd>
      </dl>
    {/if}
  </section>

  {#if isLocal}
    <section class="card section">
      <h2>Password</h2>
      <p class="hint">Changing it signs you out everywhere else.</p>
      <form class="fields" on:submit|preventDefault={changePassword}>
        <label>
          Current password
          <input
            class="input"
            type="password"
            autocomplete="current-password"
            bind:value={currentPassword}
            required
          />
        </label>
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
        <PasswordRules bind:policy />
        {#if passwordError}
          <p class="error">{passwordError}</p>
        {:else if passwordSaved}
          <p class="ok" role="status">Password changed.</p>
        {/if}
        <button class="btn btn-primary" type="submit" disabled={passwordSaving}>
          {passwordSaving ? 'Saving…' : 'Change password'}
        </button>
      </form>
    </section>

    <MfaPanel />
  {/if}

  <SessionsPanel />
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 720px;
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  h2 {
    font-size: 1.1rem;
    margin: 0;
    color: var(--text);
  }
  .section {
    padding: 1.25rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .fields {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    max-width: 380px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .fields .btn {
    align-self: flex-start;
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.35rem 1rem;
    margin: 0;
    font-size: 0.88rem;
  }
  .facts dt {
    color: var(--text-faint);
  }
  .facts dd {
    margin: 0;
    color: var(--text);
  }
  .error {
    color: var(--danger);
    margin: 0;
    font-size: 0.85rem;
  }
  .ok {
    color: var(--success);
    margin: 0;
    font-size: 0.85rem;
  }
</style>
