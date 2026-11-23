<script lang="ts">
  import SystemSettingsPage from '../lib/components/SystemSettingsPage.svelte'
  import { failureText, saveSystemSettings } from '../lib/system-settings-page'
  import type { SystemSettings } from '../lib/types'

  let saving = false

  // Live alerts (#305).
  let alertsIntervalInput = '60'
  let alertsIntervalSaved = 60
  let alertsIntervalError = ''

  function apply(settings: SystemSettings) {
    alertsIntervalSaved = settings.alerts_interval_seconds
    alertsIntervalInput = String(alertsIntervalSaved)
  }

  async function saveAlertsInterval() {
    const parsed = Number(alertsIntervalInput)
    if (!Number.isInteger(parsed) || parsed < 15 || parsed > 3600) {
      alertsIntervalError = 'Enter a whole number of seconds from 15 to 3600.'
      return
    }
    saving = true
    alertsIntervalError = ''
    try {
      apply(await saveSystemSettings({ alerts_interval_seconds: parsed }))
    } catch (err) {
      alertsIntervalError = failureText(err, 'Failed to update the alert interval')
    } finally {
      saving = false
    }
  }
</script>

<SystemSettingsPage title="Alerts" {apply}>
  <section class="card">
    <h2 class="section-title">Alerts</h2>
    <div class="setting-row column">
      <div>
        <div class="setting-name">Check alerts every</div>
        <p class="hint">How often each alert reads the lines added to its files since the last check. From 15 seconds to an hour.</p>
      </div>
      {#if alertsIntervalError}
        <p class="error">{alertsIntervalError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="15"
          max="3600"
          step="1"
          aria-label="Alert check interval in seconds"
          bind:value={alertsIntervalInput}
        />
        <span class="hint">seconds</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={saveAlertsInterval}
          disabled={saving || Number(alertsIntervalInput) === alertsIntervalSaved}
        >
          Save
        </button>
      </div>
    </div>
  </section>
</SystemSettingsPage>
