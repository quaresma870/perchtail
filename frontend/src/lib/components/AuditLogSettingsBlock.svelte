<script lang="ts">
  import { onMount } from 'svelte'
  import { currentUser } from '../auth'
  import {
    AUDIT_RETENTION_MIN_DAYS,
    LOAD_FAILED,
    SAVE_FAILED,
    failureText,
    loadSystemSettings,
    saveSystemSettings,
  } from '../system-settings-page'
  import type { SystemSettings } from '../types'

  // The audit log's settings, on the Audit log page, for whoever manages
  // system settings: whether the page's viewer is on, folder listings, and
  // how long entries are kept.

  let loading = true
  let saving = false
  let error = ''

  let auditViewEnabled = true
  let auditFolderListings = true
  // Turning folder listings off takes a super admin (the server refuses
  // anyone else), so for others the switch stays put while it's on.
  const AUDIT_FOLDER_LISTINGS_LOCKED_HINT = 'Only a super admin can turn it off.'
  $: auditFolderListingsLocked = auditFolderListings && !$currentUser?.is_super_admin
  let auditRetentionDays = 365
  let retentionInput = '365'
  let retentionSaving = false
  let retentionError = ''

  function apply(settings: SystemSettings) {
    auditViewEnabled = settings.audit_view_enabled
    auditFolderListings = settings.audit_folder_listings
    auditRetentionDays = settings.audit_retention_days
    retentionInput = String(settings.audit_retention_days)
  }

  onMount(async () => {
    try {
      apply(await loadSystemSettings())
    } catch (err) {
      error = failureText(err, LOAD_FAILED)
    } finally {
      loading = false
    }
  })

  async function toggleAuditView() {
    const next = !auditViewEnabled
    saving = true
    error = ''
    try {
      const settings = await saveSystemSettings({ audit_view_enabled: next })
      auditViewEnabled = settings.audit_view_enabled
    } catch (err) {
      error = failureText(err, SAVE_FAILED)
    } finally {
      saving = false
    }
  }

  async function toggleAuditFolderListings(event: Event) {
    const input = event.currentTarget as HTMLInputElement
    const next = !auditFolderListings
    saving = true
    error = ''
    try {
      const settings = await saveSystemSettings({ audit_folder_listings: next })
      auditFolderListings = settings.audit_folder_listings
    } catch (err) {
      // The switch moved before the request: put it back where the
      // setting still is.
      input.checked = auditFolderListings
      error = failureText(err, SAVE_FAILED)
    } finally {
      saving = false
    }
  }

  async function saveRetention() {
    const parsed = Number(retentionInput)
    if (
      !Number.isInteger(parsed) ||
      parsed < 0 ||
      (parsed !== 0 && parsed < AUDIT_RETENTION_MIN_DAYS)
    ) {
      retentionError = `Enter at least ${AUDIT_RETENTION_MIN_DAYS} days, or 0 to keep entries forever.`
      return
    }
    retentionError = ''
    retentionSaving = true
    try {
      const settings = await saveSystemSettings({ audit_retention_days: parsed })
      auditRetentionDays = settings.audit_retention_days
      retentionInput = String(settings.audit_retention_days)
    } catch (err) {
      retentionError = failureText(err, 'Failed to update retention')
    } finally {
      retentionSaving = false
    }
  }
</script>

<div class="settings-blocks">
  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}
    <section class="card">
      <h2 class="section-title">Audit log</h2>
      <div class="setting-row">
        <div>
          <div class="setting-name">Audit log</div>
          <p class="hint">
            The audit log page under Settings: who did what, when and from where. Turning this
            off hides the log and redirects away from the page for everyone except those who
            manage system settings, who keep its settings — it doesn't stop new events from
            being recorded, so re-enabling it later shows the full history again.
          </p>
        </div>
        <label class="switch">
          <input
            type="checkbox"
            aria-label="Audit log"
            checked={auditViewEnabled}
            disabled={saving}
            on:change={toggleAuditView}
          />
          <span class="switch-track"></span>
        </label>
      </div>

      <div class="setting-row">
        <div>
          <div class="setting-name">Audit folder listings</div>
          <p class="hint">
            Records every folder someone lists below a source's root, not only the sources they
            open and the files they open or download. Only a super admin can turn it off.
          </p>
        </div>
        <label class="switch" title={auditFolderListingsLocked ? AUDIT_FOLDER_LISTINGS_LOCKED_HINT : undefined}>
          <input
            type="checkbox"
            aria-label="Audit folder listings"
            checked={auditFolderListings}
            disabled={saving || auditFolderListingsLocked}
            on:change={toggleAuditFolderListings}
          />
          <span class="switch-track"></span>
        </label>
      </div>

      <div class="setting-row column">
        <div>
          <div class="setting-name">Audit log retention</div>
          <p class="hint">
            Entries older than this are purged automatically on a daily sweep, and the purge is
            itself recorded. At least {AUDIT_RETENTION_MIN_DAYS} days, or 0 to keep every entry
            forever. Only a super admin can shorten it.
          </p>
        </div>
        {#if retentionError}
          <p class="error">{retentionError}</p>
        {/if}
        <div class="retention-row">
          <input
            class="input retention-input"
            aria-label="Audit log retention in days"
            type="number"
            min="0"
            step="1"
            bind:value={retentionInput}
          />
          <span class="hint">days</span>
          <button
            type="button"
            class="btn btn-ghost"
            on:click={saveRetention}
            disabled={retentionSaving || Number(retentionInput) === auditRetentionDays}
          >
            {retentionSaving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </section>
  {/if}
</div>

<style>
  .settings-blocks {
    max-width: 640px;
  }
</style>
