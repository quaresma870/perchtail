<script lang="ts">
  import SystemSettingsPage from '../lib/components/SystemSettingsPage.svelte'
  import { failureText, saveSystemSettings } from '../lib/system-settings-page'
  import type { SystemSettings } from '../lib/types'

  let saving = false

  // Background source checks.
  let checkInput = '10'
  let checkSaved = 10
  let checkError = ''

  // File-name index (#304).
  let nameIndexEnabled = true
  let nameIndexInput = '15'
  let nameIndexSaved = 15
  let nameIndexError = ''

  // Credential age warning (#248), days; 0 = off.
  let credentialAgeInput = '90'
  let credentialAgeSaved = 90
  let credentialAgeError = ''

  // Folders per source.
  let maxRoots = 20
  let maxRootsInput = '20'
  let maxRootsSaving = false
  let maxRootsError = ''

  function apply(settings: SystemSettings) {
    checkSaved = settings.source_check_minutes
    checkInput = String(checkSaved)
    nameIndexEnabled = settings.name_index_enabled
    nameIndexSaved = settings.name_index_minutes
    nameIndexInput = String(nameIndexSaved)
    credentialAgeSaved = settings.credential_max_age_days
    credentialAgeInput = String(credentialAgeSaved)
    maxRoots = settings.source_max_roots
    maxRootsInput = String(settings.source_max_roots)
  }

  async function saveCheckInterval() {
    saving = true
    checkError = ''
    try {
      const settings = await saveSystemSettings({ source_check_minutes: Number(checkInput) })
      checkSaved = settings.source_check_minutes
      checkInput = String(checkSaved)
    } catch (err) {
      checkError = failureText(err, 'Failed to update the check interval')
    } finally {
      saving = false
    }
  }

  async function patchNameIndex(patch: Partial<SystemSettings>) {
    saving = true
    nameIndexError = ''
    try {
      const settings = await saveSystemSettings(patch)
      nameIndexEnabled = settings.name_index_enabled
      nameIndexSaved = settings.name_index_minutes
      nameIndexInput = String(nameIndexSaved)
    } catch (err) {
      nameIndexError = failureText(err, 'Failed to update the file-name index')
    } finally {
      saving = false
    }
  }

  async function saveCredentialAge() {
    const parsed = Number(credentialAgeInput)
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 3650) {
      credentialAgeError = 'Enter a whole number of days from 0 to 3650 (0 = off).'
      return
    }
    saving = true
    credentialAgeError = ''
    try {
      const settings = await saveSystemSettings({ credential_max_age_days: parsed })
      credentialAgeSaved = settings.credential_max_age_days
      credentialAgeInput = String(credentialAgeSaved)
    } catch (err) {
      credentialAgeError = failureText(err, 'Failed to update the credential age warning')
    } finally {
      saving = false
    }
  }

  async function saveMaxRoots() {
    const parsed = Number(maxRootsInput)
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 100) {
      maxRootsError = 'Enter a whole number from 1 to 100.'
      return
    }
    maxRootsError = ''
    maxRootsSaving = true
    try {
      const settings = await saveSystemSettings({ source_max_roots: parsed })
      maxRoots = settings.source_max_roots
      maxRootsInput = String(settings.source_max_roots)
    } catch (err) {
      maxRootsError = failureText(err, 'Failed to update the folder limit')
    } finally {
      maxRootsSaving = false
    }
  }
</script>

<SystemSettingsPage title="Source defaults" {apply}>
  <section class="card">
    <h2 class="section-title">Sources</h2>
    <div class="setting-row column">
      <div>
        <div class="setting-name">Check sources in the background</div>
        <p class="hint">
          How often PerchTail checks that each source answers (one connection per source), so
          the explorer shows which ones are down before anyone opens them. Push-agent sources
          show their live connection instead. 0 turns it off.
        </p>
      </div>
      {#if checkError}
        <p class="error">{checkError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="0"
          max="1440"
          step="1"
          aria-label="Background check interval in minutes"
          bind:value={checkInput}
        />
        <span class="hint">minutes</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={saveCheckInterval}
          disabled={saving || Number(checkInput) === checkSaved}
        >
          Save
        </button>
      </div>
    </div>

    <div class="setting-row">
      <div>
        <div class="setting-name">Index file names</div>
        <p class="hint">
          Lists every source's files in the background and keeps their names, sizes and times, never their content, so file names can be searched across sources without listing every server on each search. Each source can be left out in its own settings. Turning it off stops the listings; what was indexed stays until a source is changed.
        </p>
      </div>
      <label class="switch">
        <input
          type="checkbox"
          aria-label="Index file names"
          checked={nameIndexEnabled}
          disabled={saving}
          on:change={() => patchNameIndex({ name_index_enabled: !nameIndexEnabled })}
        />
        <span class="switch-track"></span>
      </label>
    </div>

    <div class="setting-row column">
      <div>
        <div class="setting-name">List each source's files every</div>
        <p class="hint">A source is listed again once its index is older than this, a few sources at a time. From 5 minutes to a day.</p>
      </div>
      {#if nameIndexError}
        <p class="error">{nameIndexError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="5"
          max="1440"
          step="1"
          aria-label="File-name index interval in minutes"
          disabled={!nameIndexEnabled}
          bind:value={nameIndexInput}
        />
        <span class="hint">minutes</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={() => patchNameIndex({ name_index_minutes: Number(nameIndexInput) })}
          disabled={saving || !nameIndexEnabled || Number(nameIndexInput) === nameIndexSaved}
        >
          Save
        </button>
      </div>
    </div>

    <div class="setting-row column">
      <div>
        <div class="setting-name">Warn when a credential is older than</div>
        <p class="hint">
          Settings → Sources marks the sources whose credential was set longer ago than this, so
          passwords and keys due for rotation stand out. 0 turns the warning off.
        </p>
      </div>
      {#if credentialAgeError}
        <p class="error">{credentialAgeError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="0"
          max="3650"
          step="1"
          aria-label="Credential age warning in days"
          bind:value={credentialAgeInput}
        />
        <span class="hint">days</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={saveCredentialAge}
          disabled={saving || Number(credentialAgeInput) === credentialAgeSaved}
        >
          Save
        </button>
      </div>
    </div>

    <div class="setting-row column">
      <div>
        <div class="setting-name">Folders per source</div>
        <p class="hint">
          How many folders one source may read. 1 turns several folders per source off. At most
          100. Sources that already have more keep them, but can't add more.
        </p>
      </div>
      {#if maxRootsError}
        <p class="error">{maxRootsError}</p>
      {/if}
      <div class="limit-row">
        <input
          class="input limit-input"
          type="number"
          min="1"
          max="100"
          step="1"
          aria-label="Folders per source"
          bind:value={maxRootsInput}
        />
        <span class="hint">folders</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={saveMaxRoots}
          disabled={maxRootsSaving || Number(maxRootsInput) === maxRoots}
        >
          {maxRootsSaving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  </section>
</SystemSettingsPage>
