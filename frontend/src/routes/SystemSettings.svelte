<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import UpdatesCard from '../lib/components/UpdatesCard.svelte'
  import { systemSettings } from '../lib/settings'
  import { EXPLORER_SORTS } from '../lib/explorer-sort'
  import { DEFAULT_TAIL_INTERVAL_SECONDS, TAIL_INTERVALS_SECONDS } from '../lib/follow'
  import type {
    ExplorerSort,
    MonitoringTokenResult,
    MonitoringTokenStatus,
    SystemSettings,
  } from '../lib/types'

  let loading = true
  let saving = false
  let error = ''

  let auditViewEnabled = true
  let auditRetentionDays = 365
  let retentionInput = '365'
  let connectionsViewDefault: 'list' | 'tree' = 'list'
  let connectionsViewSwitchable = true
  let retentionSaving = false
  let retentionError = ''
  let maxRoots = 20
  let maxRootsInput = '20'
  let maxRootsSaving = false
  let maxRootsError = ''

  // Password policy (#245), saved together with one button.
  const POLICY_KEYS = [
    'password_min_length',
    'password_max_length',
    'password_reject_simple',
    'password_reject_personal',
    'password_history',
    'password_required_classes',
    'password_max_age_days',
  ] as const
  type PolicyFields = Pick<SystemSettings, (typeof POLICY_KEYS)[number]>
  let savedPolicy: PolicyFields | null = null
  let policyDraft: PolicyFields | null = null
  let policySaving = false
  let policyError = ''
  const pickPolicy = (settings: SystemSettings): PolicyFields =>
    Object.fromEntries(POLICY_KEYS.map((key) => [key, settings[key]])) as PolicyFields
  $: policyDirty =
    savedPolicy !== null &&
    policyDraft !== null &&
    POLICY_KEYS.some((key) => savedPolicy?.[key] !== policyDraft?.[key])

  let monitoringConfigured = false
  let monitoringToken = ''
  let monitoringTokenError = ''
  let generatingMonitoringToken = false

  onMount(async () => {
    try {
      const settings = await api.get<SystemSettings>('/system-settings')
      auditViewEnabled = settings.audit_view_enabled
      auditRetentionDays = settings.audit_retention_days
      retentionInput = String(settings.audit_retention_days)
      maxRoots = settings.source_max_roots
      maxRootsInput = String(settings.source_max_roots)
      connectionsViewDefault = settings.connections_view_default
      connectionsViewSwitchable = settings.connections_view_switchable
      explorerSortDefault = settings.explorer_sort_default
      tailIntervalSeconds = settings.tail_interval_seconds
      refreshSaved = settings.explorer_refresh_seconds
      refreshInput = String(refreshSaved)
      checkSaved = settings.source_check_minutes
      checkInput = String(checkSaved)
      nameIndexEnabled = settings.name_index_enabled
      nameIndexSaved = settings.name_index_minutes
      nameIndexInput = String(nameIndexSaved)
      alertsIntervalSaved = settings.alerts_interval_seconds
      alertsIntervalInput = String(alertsIntervalSaved)
      credentialAgeSaved = settings.credential_max_age_days
      credentialAgeInput = String(credentialAgeSaved)
      savedPolicy = pickPolicy(settings)
      policyDraft = { ...savedPolicy }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load system settings'
    } finally {
      loading = false
    }

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
      monitoringTokenError = err instanceof ApiError ? err.detail : 'Failed to generate token'
    } finally {
      generatingMonitoringToken = false
    }
  }

  // Explorer (#272, #273).
  let explorerSortDefault: ExplorerSort = 'name_asc'
  let tailIntervalSeconds: number = DEFAULT_TAIL_INTERVAL_SECONDS
  let refreshInput = '30'
  let refreshSaved = 30
  let refreshError = ''
  let checkInput = '10'
  let checkSaved = 10
  let checkError = ''

  // File-name index (#304).
  let nameIndexEnabled = true
  let nameIndexInput = '15'
  let nameIndexSaved = 15
  let nameIndexError = ''

  async function patchNameIndex(patch: Partial<SystemSettings>) {
    saving = true
    nameIndexError = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', patch)
      nameIndexEnabled = settings.name_index_enabled
      nameIndexSaved = settings.name_index_minutes
      nameIndexInput = String(nameIndexSaved)
      systemSettings.set(settings)
    } catch (err) {
      nameIndexError = err instanceof ApiError ? err.detail : 'Failed to update the file-name index'
    } finally {
      saving = false
    }
  }

  // Live alerts (#305).
  let alertsIntervalInput = '60'
  let alertsIntervalSaved = 60
  let alertsIntervalError = ''

  async function saveAlertsInterval() {
    const parsed = Number(alertsIntervalInput)
    if (!Number.isInteger(parsed) || parsed < 15 || parsed > 3600) {
      alertsIntervalError = 'Enter a whole number of seconds from 15 to 3600.'
      return
    }
    saving = true
    alertsIntervalError = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        alerts_interval_seconds: parsed,
      })
      alertsIntervalSaved = settings.alerts_interval_seconds
      alertsIntervalInput = String(alertsIntervalSaved)
      systemSettings.set(settings)
    } catch (err) {
      alertsIntervalError = err instanceof ApiError ? err.detail : 'Failed to update the alert interval'
    } finally {
      saving = false
    }
  }

  // Credential age warning (#248), days; 0 = off.
  let credentialAgeInput = '90'
  let credentialAgeSaved = 90
  let credentialAgeError = ''

  async function saveCredentialAge() {
    const parsed = Number(credentialAgeInput)
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 3650) {
      credentialAgeError = 'Enter a whole number of days from 0 to 3650 (0 = off).'
      return
    }
    saving = true
    credentialAgeError = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        credential_max_age_days: parsed,
      })
      credentialAgeSaved = settings.credential_max_age_days
      credentialAgeInput = String(credentialAgeSaved)
      systemSettings.set(settings)
    } catch (err) {
      credentialAgeError =
        err instanceof ApiError ? err.detail : 'Failed to update the credential age warning'
    } finally {
      saving = false
    }
  }

  async function saveCheckInterval() {
    saving = true
    checkError = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        source_check_minutes: Number(checkInput),
      })
      checkSaved = settings.source_check_minutes
      checkInput = String(checkSaved)
      systemSettings.set(settings)
    } catch (err) {
      checkError = err instanceof ApiError ? err.detail : 'Failed to update the check interval'
    } finally {
      saving = false
    }
  }

  async function patchExplorer(patch: Partial<SystemSettings>) {
    saving = true
    error = ''
    refreshError = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', patch)
      explorerSortDefault = settings.explorer_sort_default
      tailIntervalSeconds = settings.tail_interval_seconds
      refreshSaved = settings.explorer_refresh_seconds
      refreshInput = String(refreshSaved)
      systemSettings.set(settings)
    } catch (err) {
      const message = err instanceof ApiError ? err.detail : 'Failed to update system settings'
      if ('explorer_refresh_seconds' in patch) refreshError = message
      else error = message
    } finally {
      saving = false
    }
  }

  async function patchConnectionsView(patch: Partial<SystemSettings>) {
    saving = true
    error = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', patch)
      connectionsViewDefault = settings.connections_view_default
      connectionsViewSwitchable = settings.connections_view_switchable
      systemSettings.set(settings)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update system settings'
    } finally {
      saving = false
    }
  }

  async function toggleAuditView() {
    const next = !auditViewEnabled
    saving = true
    error = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        audit_view_enabled: next,
      })
      auditViewEnabled = settings.audit_view_enabled
      systemSettings.set(settings)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update system settings'
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
      const settings = await api.patch<SystemSettings>('/system-settings', {
        source_max_roots: parsed,
      })
      maxRoots = settings.source_max_roots
      maxRootsInput = String(settings.source_max_roots)
      systemSettings.set(settings)
    } catch (err) {
      maxRootsError = err instanceof ApiError ? err.detail : 'Failed to update the folder limit'
    } finally {
      maxRootsSaving = false
    }
  }

  async function savePolicy() {
    if (!policyDraft) return
    policyError = ''
    policySaving = true
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', policyDraft)
      savedPolicy = pickPolicy(settings)
      policyDraft = { ...savedPolicy }
      systemSettings.set(settings)
    } catch (err) {
      policyError = err instanceof ApiError ? err.detail : 'Failed to update the password policy'
    } finally {
      policySaving = false
    }
  }

  async function saveRetention() {
    const parsed = Number(retentionInput)
    if (!Number.isInteger(parsed) || parsed < 0) {
      retentionError = 'Enter a whole number of days (0 = keep forever).'
      return
    }
    retentionError = ''
    retentionSaving = true
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', {
        audit_retention_days: parsed,
      })
      auditRetentionDays = settings.audit_retention_days
      retentionInput = String(settings.audit_retention_days)
      systemSettings.set(settings)
    } catch (err) {
      retentionError = err instanceof ApiError ? err.detail : 'Failed to update retention'
    } finally {
      retentionSaving = false
    }
  }
</script>

<SettingsNav />

<div class="page">
  <h1>System settings</h1>
  <p class="hint">
    Deployment-wide feature toggles — these hide or show a view for every user, separate from what
    a role can access.
  </p>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="grid">
    <section class="card">
      <h2 class="section-title">Explorer</h2>
      <div class="setting-row">
        <div>
          <div class="setting-name">Connections view</div>
          <p class="hint">
            How the viewer's explorer lists sources by default, for everyone: a flat list, or a
            tree by customer and folder. People who already have PerchTail open see the change
            on their next page change, within 30 seconds.
          </p>
        </div>
        <select
          class="input view-select"
          value={connectionsViewDefault}
          disabled={saving}
          on:change={(e) =>
            patchConnectionsView({
              connections_view_default: e.currentTarget.value === 'tree' ? 'tree' : 'list',
            })}
        >
          <option value="list">List</option>
          <option value="tree">Tree</option>
        </select>
      </div>

      <div class="setting-row">
        <div>
          <div class="setting-name">Let users switch between list and tree</div>
          <p class="hint">
            When on, each user can switch and the choice follows them (kept on the server). When
            off, the switch is hidden and everyone sees the default view above.
          </p>
        </div>
        <label class="switch">
          <input
            type="checkbox"
            checked={connectionsViewSwitchable}
            disabled={saving}
            on:change={() =>
              patchConnectionsView({ connections_view_switchable: !connectionsViewSwitchable })}
          />
          <span class="switch-track"></span>
        </label>
      </div>

      <div class="setting-row">
        <div>
          <div class="setting-name">File order</div>
          <p class="hint">How the explorer orders the files in a folder. Folders always come first.</p>
        </div>
        <select
          class="input view-select"
          aria-label="Default file order"
          value={explorerSortDefault}
          disabled={saving}
          on:change={(e) =>
            patchExplorer({ explorer_sort_default: e.currentTarget.value as ExplorerSort })}
        >
          {#each EXPLORER_SORTS as option}
            <option value={option.value}>{option.label}</option>
          {/each}
        </select>
      </div>

      <div class="setting-row column">
        <div>
          <div class="setting-name">Refresh open folders</div>
          <p class="hint">
            How often the explorer lists the folders people have open again, so files that
            rotate in and out show up -- only the list of files, never their content, and not
            while the browser tab is hidden. 0 turns it off; otherwise at least 5 seconds.
          </p>
        </div>
        {#if refreshError}
          <p class="error">{refreshError}</p>
        {/if}
        <div class="interval-row">
          <input
            class="input interval-input"
            type="number"
            min="0"
            max="3600"
            step="1"
            aria-label="Refresh interval in seconds"
            bind:value={refreshInput}
          />
          <span class="hint">seconds</span>
          <button
            type="button"
            class="btn btn-ghost"
            on:click={() => patchExplorer({ explorer_refresh_seconds: Number(refreshInput) })}
            disabled={saving || Number(refreshInput) === refreshSaved}
          >
            Save
          </button>
        </div>
      </div>

      <div class="setting-row">
        <div>
          <div class="setting-name">Tail interval</div>
          <p class="hint">
            How often Tail checks an open file for new lines. Each check reads only what was
            appended; a shorter interval is more live but asks the source more often.
          </p>
        </div>
        <select
          class="input view-select"
          aria-label="Tail interval"
          value={tailIntervalSeconds}
          disabled={saving}
          on:change={(e) => patchExplorer({ tail_interval_seconds: Number(e.currentTarget.value) })}
        >
          {#each TAIL_INTERVALS_SECONDS as seconds}
            <option value={seconds}>{seconds} s</option>
          {/each}
        </select>
      </div>
    </section>

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

    {#if $currentUser?.is_super_admin}
      <UpdatesCard />
    {/if}

    <section class="card">
      <h2 class="section-title">Audit log</h2>
      <div class="setting-row">
        <div>
          <div class="setting-name">Audit log</div>
          <p class="hint">
            The audit log page under Settings, listing logins and every source/rule/role/user/
            customer/folder/SSO/system-settings change. Turning this off hides the page and
            redirects away from it for every user — it doesn't stop new events from being
            recorded, so re-enabling it later shows the full history again.
          </p>
        </div>
        <label class="switch">
          <input
            type="checkbox"
            checked={auditViewEnabled}
            disabled={saving}
            on:change={toggleAuditView}
          />
          <span class="switch-track"></span>
        </label>
      </div>

      <div class="setting-row column">
        <div>
          <div class="setting-name">Audit log retention</div>
          <p class="hint">
            Entries older than this are purged automatically on a daily sweep. 0 keeps every entry
            forever.
          </p>
        </div>
        {#if retentionError}
          <p class="error">{retentionError}</p>
        {/if}
        <div class="retention-row">
          <input
            class="input retention-input"
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
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .card {
    display: flex;
    flex-direction: column;
  }
  /* Settings are grouped in cards that fill the page width (#357): two or
     three columns on a wide screen, one on a narrow one. */
  .grid {
    columns: 3 420px;
    column-gap: 1.1rem;
  }
  .grid > :global(*) {
    break-inside: avoid;
    margin-bottom: 1.1rem;
  }
  .section-title {
    font-size: 0.8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    margin: 0.9rem 1.1rem 0.2rem;
  }
  .setting-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1.5rem;
    padding: 1.1rem 1.5rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .setting-row:last-child {
    border-bottom: none;
  }
  .setting-name {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.25rem;
  }
  .setting-row .switch {
    flex: 0 0 auto;
    margin-top: 0.1rem;
  }
  .setting-row.column {
    flex-direction: column;
    align-items: flex-start;
    gap: 0.75rem;
  }
  .setting-row.column .btn {
    align-self: flex-start;
  }
  .retention-row,
  .interval-row,
  .limit-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .retention-input,
  .interval-input,
  .limit-input {
    width: 6rem;
  }
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
  .error {
    color: var(--danger);
    margin: 0;
  }
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
  .view-select {
    width: auto;
    min-width: 7rem;
  }
</style>
