<script lang="ts">
  import { currentUser, refreshCurrentUser } from '../lib/auth'
  import SystemSettingsPage from '../lib/components/SystemSettingsPage.svelte'
  import {
    SAVE_FAILED,
    failureText,
    pickPolicy,
    policyChanged,
    saveSystemSettings,
    type PolicyFields,
  } from '../lib/system-settings-page'
  import type { SystemSettings } from '../lib/types'

  let saving = false
  let error = ''

  // Who must use MFA is a super admin's call, both ways (the server refuses
  // anyone else).
  let requireMfa = false
  const REQUIRE_MFA_LOCKED_HINT = 'Only a super admin can change it.'
  $: requireMfaLocked = !$currentUser?.is_super_admin
  // Password recovery by email (#244): super admins only, and only while
  // email is set up and on and PUBLIC_BASE_URL is https (the server says why
  // not in password_reset_unavailable).
  let passwordResetEnabled = false
  let passwordResetUnavailable: string | null = null
  let passwordResetMinutesSaved = 30
  let passwordResetMinutesInput = '30'
  let passwordResetError = ''
  $: passwordResetLocked = !$currentUser?.is_super_admin

  // Password policy (#245), saved together with one button.
  let savedPolicy: PolicyFields | null = null
  let policyDraft: PolicyFields | null = null
  let policySaving = false
  let policyError = ''
  $: policyDirty = policyChanged(savedPolicy, policyDraft)

  function apply(settings: SystemSettings) {
    requireMfa = settings.require_mfa_local_accounts
    applyPasswordReset(settings)
    savedPolicy = pickPolicy(settings)
    policyDraft = { ...savedPolicy }
  }

  async function toggleRequireMfa(event: Event) {
    const input = event.currentTarget as HTMLInputElement
    const next = !requireMfa
    saving = true
    error = ''
    try {
      const settings = await saveSystemSettings({ require_mfa_local_accounts: next })
      requireMfa = settings.require_mfa_local_accounts
      // Turning it on may cover this account too.
      await refreshCurrentUser()
    } catch (err) {
      input.checked = requireMfa
      error = failureText(err, SAVE_FAILED)
    } finally {
      saving = false
    }
  }

  function applyPasswordReset(settings: SystemSettings) {
    passwordResetEnabled = settings.password_reset_enabled
    passwordResetUnavailable = settings.password_reset_unavailable
    passwordResetMinutesSaved = settings.password_reset_minutes
    passwordResetMinutesInput = String(settings.password_reset_minutes)
  }

  async function patchPasswordReset(patch: Partial<SystemSettings>, input?: HTMLInputElement) {
    saving = true
    passwordResetError = ''
    try {
      applyPasswordReset(await saveSystemSettings(patch))
    } catch (err) {
      if (input) input.checked = passwordResetEnabled
      passwordResetError = failureText(err, 'Failed to update password recovery')
    } finally {
      saving = false
    }
  }

  function savePasswordResetMinutes() {
    const parsed = Number(passwordResetMinutesInput)
    if (!Number.isInteger(parsed) || parsed < 5 || parsed > 60) {
      passwordResetError = 'Enter a whole number of minutes from 5 to 60.'
      return
    }
    patchPasswordReset({ password_reset_minutes: parsed })
  }

  async function savePolicy() {
    if (!policyDraft) return
    policyError = ''
    policySaving = true
    try {
      savedPolicy = pickPolicy(await saveSystemSettings(policyDraft))
      policyDraft = { ...savedPolicy }
    } catch (err) {
      policyError = failureText(err, 'Failed to update the password policy')
    } finally {
      policySaving = false
    }
  }
</script>

<SystemSettingsPage title="Sign-in security" {apply} {error}>
  <section class="card">
    <h2 class="section-title">Two-factor authentication</h2>
    <div class="setting-row">
      <div>
        <div class="setting-name">Require MFA for local accounts</div>
        <p class="hint">
          Super admins and roles with any global capability must always use two-factor
          authentication. This asks every other local account for it too; SSO accounts use their
          identity provider's. Anyone covered without it can only set it up until they do. Only
          a super admin can change it.
        </p>
      </div>
      <label class="switch" title={requireMfaLocked ? REQUIRE_MFA_LOCKED_HINT : undefined}>
        <input
          type="checkbox"
          aria-label="Require MFA for local accounts"
          checked={requireMfa}
          disabled={saving || requireMfaLocked}
          on:change={toggleRequireMfa}
        />
        <span class="switch-track"></span>
      </label>
    </div>
  </section>

  <section class="card">
    <h2 class="section-title">Password recovery</h2>
    <div class="setting-row">
      <div>
        <div class="setting-name">Password recovery by email</div>
        <p class="hint">
          Shows <strong>Forgot password?</strong> on the sign-in page: users of local accounts get
          a link by email to choose a new password, which signs them out everywhere. Super admins,
          roles with any global capability, SSO accounts and accounts without an email can't use
          it. Needs email under Settings → Email and an https PUBLIC_BASE_URL. Only a super admin
          can change it.
        </p>
        {#if passwordResetUnavailable}
          <p class="warning" role="status">
            {passwordResetEnabled ? "It's on but doesn't work now: " : 'Unavailable: '}
            {passwordResetUnavailable}.
          </p>
        {/if}
      </div>
      <label class="switch" title={passwordResetLocked ? REQUIRE_MFA_LOCKED_HINT : undefined}>
        <input
          type="checkbox"
          aria-label="Password recovery by email"
          checked={passwordResetEnabled}
          disabled={saving ||
            passwordResetLocked ||
            (!passwordResetEnabled && passwordResetUnavailable !== null)}
          on:change={(e) =>
            patchPasswordReset({ password_reset_enabled: !passwordResetEnabled }, e.currentTarget)}
        />
        <span class="switch-track"></span>
      </label>
    </div>
    <div class="setting-row column">
      <div>
        <div class="setting-name">Links work for</div>
        <p class="hint">How long a reset link works after it's sent, from 5 to 60 minutes.</p>
      </div>
      {#if passwordResetError}
        <p class="error">{passwordResetError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="5"
          max="60"
          step="1"
          aria-label="Reset links work for, in minutes"
          bind:value={passwordResetMinutesInput}
          disabled={passwordResetLocked}
        />
        <span class="hint">minutes</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={savePasswordResetMinutes}
          disabled={saving ||
            passwordResetLocked ||
            Number(passwordResetMinutesInput) === passwordResetMinutesSaved}
        >
          Save
        </button>
      </div>
    </div>
  </section>

  {#if policyDraft}
    <div class="card">
      <div class="setting-row column">
        <div>
          <div class="setting-name">Password policy</div>
          <p class="hint">
            For local accounts, checked whenever a password is set: a user's own change, the
            change at first login, and temporary passwords an admin sets. Existing passwords are
            checked at their next change. The defaults follow NIST SP 800-63B: length first,
            no composition rules or expiry.
          </p>
        </div>
        <div class="policy-grid">
          <label for="policy-min">Minimum length</label>
          <input
            id="policy-min"
            class="input policy-input"
            type="number"
            min="8"
            max="1024"
            bind:value={policyDraft.password_min_length}
          />
          <label for="policy-max">Maximum length</label>
          <input
            id="policy-max"
            class="input policy-input"
            type="number"
            min="64"
            max="1024"
            bind:value={policyDraft.password_max_length}
          />
          <label for="policy-classes">Character classes required</label>
          <select
            id="policy-classes"
            class="input policy-input"
            bind:value={policyDraft.password_required_classes}
          >
            <option value={0}>off</option>
            <option value={1}>1 of 4</option>
            <option value={2}>2 of 4</option>
            <option value={3}>3 of 4</option>
            <option value={4}>all 4</option>
          </select>
          <label for="policy-history">Don't reuse the last N passwords (0 = off)</label>
          <input
            id="policy-history"
            class="input policy-input"
            type="number"
            min="0"
            max="24"
            bind:value={policyDraft.password_history}
          />
          <label for="policy-age">Expire after N days (0 = never)</label>
          <input
            id="policy-age"
            class="input policy-input"
            type="number"
            min="0"
            max="3650"
            bind:value={policyDraft.password_max_age_days}
          />
        </div>
        <label class="check">
          <input type="checkbox" bind:checked={policyDraft.password_reject_simple} />
          Reject passwords that are too simple (repeats, sequences, keyboard runs, the username)
        </label>
        <label class="check">
          <input type="checkbox" bind:checked={policyDraft.password_reject_personal} />
          Reject passwords containing the username, email or name
        </label>
        {#if policyError}
          <p class="error">{policyError}</p>
        {/if}
        <button
          type="button"
          class="btn btn-ghost"
          on:click={savePolicy}
          disabled={policySaving || !policyDirty}
        >
          {policySaving ? 'Saving…' : 'Save policy'}
        </button>
      </div>
    </div>
  {/if}
</SystemSettingsPage>

<style>
  .policy-grid {
    display: grid;
    grid-template-columns: 1fr 7rem;
    gap: 0.5rem 1rem;
    align-items: center;
    font-size: 0.86rem;
    color: var(--text);
    width: 100%;
  }
  .policy-input {
    width: 7rem;
  }
  .check {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    font-size: 0.86rem;
    color: var(--text);
  }
</style>
