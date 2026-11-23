<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../lib/api'
  import SystemSettingsPage from '../lib/components/SystemSettingsPage.svelte'
  import { failureText } from '../lib/system-settings-page'
  import type { MonitoringTokenResult, MonitoringTokenStatus } from '../lib/types'

  let monitoringConfigured = false
  let monitoringToken = ''
  let monitoringTokenError = ''
  let generatingMonitoringToken = false

  // Nothing on this page comes from /system-settings itself.
  const apply = () => {}

  onMount(async () => {
    try {
      const status = await api.get<MonitoringTokenStatus>('/monitoring/token')
      monitoringConfigured = status.configured
    } catch {
      // Non-fatal -- the token section just starts assuming unconfigured.
    }
  })

  async function generateMonitoringToken() {
    monitoringTokenError = ''
    generatingMonitoringToken = true
    try {
      const result = await api.post<MonitoringTokenResult>('/monitoring/token')
      monitoringToken = result.token
      monitoringConfigured = true
    } catch (err) {
      monitoringTokenError = failureText(err, 'Failed to generate token')
    } finally {
      generatingMonitoringToken = false
    }
  }
</script>

<SystemSettingsPage title="Monitoring" {apply}>
  <div class="card">
    <div class="setting-row column">
      <div>
        <div class="setting-name">Monitoring</div>
        <p class="hint">
          Bearer token for external monitoring (Zabbix, Prometheus, etc.) to poll
          <code>GET /monitoring/health</code> — separate from user login, since a monitoring
          system can't do an interactive session. The token is shown once, right after it's
          generated; regenerating it invalidates whatever token was issued before.
        </p>
      </div>
      {#if monitoringTokenError}
        <p class="error">{monitoringTokenError}</p>
      {/if}
      <button
        type="button"
        class="btn btn-ghost"
        on:click={generateMonitoringToken}
        disabled={generatingMonitoringToken}
      >
        {generatingMonitoringToken
          ? 'Generating…'
          : monitoringConfigured
            ? 'Regenerate token'
            : 'Generate token'}
      </button>
      {#if monitoringToken}
        <code class="token-box">{monitoringToken}</code>
      {/if}
    </div>
  </div>
</SystemSettingsPage>

<style>
  .token-box {
    display: block;
    padding: 0.6rem 0.75rem;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    word-break: break-all;
    width: 100%;
  }
</style>
