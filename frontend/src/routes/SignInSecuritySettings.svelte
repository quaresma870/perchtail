<script lang="ts">
  import { api } from '../lib/api'
  import { currentUser, refreshCurrentUser } from '../lib/auth'
  import { confirmAction } from '../lib/confirm'
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

  // Sessions: how long an unused one lasts (0 = no limit, else 5 to 720
  // minutes), and, for a super admin, ending every other one.
  let idleSaved = 60
  let idleInput = '60'
  let idleError = ''
  let endedMessage = ''
  // Words no password may contain, one per line.
  let wordsSaved = ''
  let wordsInput = ''
  let wordsError = ''

  function apply(settings: SystemSettings) {
    idleSaved = settings.session_idle_minutes
    idleInput = String(settings.session_idle_minutes)
    wordsSaved = settings.password_context_words.join('\n')
    wordsInput = wordsSaved
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

  async function saveIdle() {
    const parsed = Number(idleInput)
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 720 || (parsed > 0 && parsed < 5)) {
      idleError = 'Enter 0 for no limit, or a whole number of minutes from 5 to 720.'
      return
    }
    idleError = ''
    saving = true
    try {
      const settings = await saveSystemSettings({ session_idle_minutes: parsed })
      idleSaved = settings.session_idle_minutes
      idleInput = String(idleSaved)
    } catch (err) {
      idleError = failureText(err, 'Failed to update the session timeout')
    } finally {
      saving = false
    }
  }

  async function endEveryOtherSession() {
    const ok = await confirmAction({
      title: 'End every other session',
      message:
        'Everyone signed in, on every device, is signed out and must sign in again. Your own session stays.',
      confirmLabel: 'End sessions',
      danger: true,
    })
    if (!ok) return
    endedMessage = ''
    error = ''
    try {
      const { count } = await api.post<{ count: number }>('/users/sessions/revoke-all', {})
      endedMessage = count === 1 ? '1 session ended.' : `${count} sessions ended.`
    } catch (err) {
      error = failureText(err, 'Failed to end the sessions')
    }
  }

  async function saveWords() {
    const words = wordsInput
      .split('\n')
      .map((word) => word.trim())
      .filter(Boolean)
    wordsError = ''
    saving = true
    try {
      const settings = await saveSystemSettings({ password_context_words: words })
      wordsSaved = settings.password_context_words.join('\n')
      wordsInput = wordsSaved
    } catch (err) {
      wordsError = failureText(err, 'Failed to update the words')
    } finally {
      saving = false
    }
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
    <h2 class="section-title">Sessions</h2>
    <div class="setting-row column">
      <div>
        <div class="setting-name">End unused sessions after</div>
        <p class="hint">
          A session nobody used for this long ends, and its user signs in again. Any request counts
          as use, a running Tail included. 0 keeps sessions until they expire.
        </p>
      </div>
      {#if idleError}
        <p class="error">{idleError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="0"
          max="720"
          step="1"
          aria-label="End unused sessions after, in minutes"
          bind:value={idleInput}
        />
        <span class="hint">minutes</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={saveIdle}
          disabled={saving || Number(idleInput) === idleSaved}
        >
          Save
        </button>
      </div>
    </div>
    {#if $currentUser?.is_super_admin}
      <div class="setting-row column">
        <div>
          <div class="setting-name">End every other session</div>
          <p class="hint">Signs out everyone, on every device, except you. Super admins only.</p>
          {#if endedMessage}
            <p class="hint" role="status">{endedMessage}</p>
          {/if}
        </div>
        <button type="button" class="btn btn-ghost" on:click={endEveryOtherSession}>
          End every other session
        </button>
      </div>
    {/if}
  </section>

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

  <section class="card">
    <h2 class="section-title">Words passwords can't contain</h2>
    <div class="setting-row column">
      <div>
        <p class="hint">
          One per line, 3 to 64 characters each, up to 50: your organization's name, for example. A
          new password containing one, in any case, is refused, and so is one containing
          "perchtail".
        </p>
      </div>
      {#if wordsError}
        <p class="error">{wordsError}</p>
      {/if}
      <textarea
        class="input words-input"
        rows="4"
        aria-label="Words passwords can't contain, one per line"
        bind:value={wordsInput}
      ></textarea>
      <button
        type="button"
        class="btn btn-ghost"
        on:click={saveWords}
        disabled={saving || wordsInput.trim() === wordsSaved.trim()}
      >
        Save words
      </button>
    </div>
  </section>
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
  .words-input {
    width: 100%;
    max-width: 28rem;
    font-family: inherit;
  }
  .check {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    font-size: 0.86rem;
    color: var(--text);
  }
</style>
