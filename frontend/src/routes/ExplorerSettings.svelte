<script lang="ts">
  import SystemSettingsPage from '../lib/components/SystemSettingsPage.svelte'
  import { EXPLORER_SORTS } from '../lib/explorer-sort'
  import { DEFAULT_TAIL_INTERVAL_SECONDS, TAIL_INTERVALS_SECONDS } from '../lib/follow'
  import {
    LIMIT_FIELDS,
    SAVE_FAILED,
    failureText,
    limitsChanged,
    limitsPatch,
    limitsProblem,
    pickLimits,
    saveSystemSettings,
    type LimitFields,
  } from '../lib/system-settings-page'
  import type { ExplorerSort, SystemSettings } from '../lib/types'

  let saving = false
  let error = ''

  // Explorer (#272, #273).
  let connectionsViewDefault: 'list' | 'tree' = 'list'
  let connectionsViewSwitchable = true
  let explorerSortDefault: ExplorerSort = 'name_asc'
  let tailIntervalSeconds: number = DEFAULT_TAIL_INTERVAL_SECONDS
  let refreshInput = '30'
  let refreshSaved = 30
  let refreshError = ''

  // Server-side limits (#344, #347), saved together with one button.
  let savedLimits: LimitFields | null = null
  let limitsDraft: LimitFields | null = null
  let limitsSaving = false
  let limitsError = ''
  $: limitsDirty = limitsChanged(savedLimits, limitsDraft)

  function apply(settings: SystemSettings) {
    connectionsViewDefault = settings.connections_view_default
    connectionsViewSwitchable = settings.connections_view_switchable
    explorerSortDefault = settings.explorer_sort_default
    tailIntervalSeconds = settings.tail_interval_seconds
    refreshSaved = settings.explorer_refresh_seconds
    refreshInput = String(refreshSaved)
    savedLimits = pickLimits(settings)
    limitsDraft = { ...savedLimits }
  }

  async function patchExplorer(patch: Partial<SystemSettings>) {
    saving = true
    error = ''
    refreshError = ''
    try {
      const settings = await saveSystemSettings(patch)
      explorerSortDefault = settings.explorer_sort_default
      tailIntervalSeconds = settings.tail_interval_seconds
      refreshSaved = settings.explorer_refresh_seconds
      refreshInput = String(refreshSaved)
    } catch (err) {
      const message = failureText(err, SAVE_FAILED)
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
      const settings = await saveSystemSettings(patch)
      connectionsViewDefault = settings.connections_view_default
      connectionsViewSwitchable = settings.connections_view_switchable
    } catch (err) {
      error = failureText(err, SAVE_FAILED)
    } finally {
      saving = false
    }
  }

  async function saveLimits() {
    if (!limitsDraft) return
    const problem = limitsProblem(limitsDraft)
    if (problem) {
      limitsError = problem
      return
    }
    limitsError = ''
    limitsSaving = true
    try {
      savedLimits = pickLimits(await saveSystemSettings(limitsPatch(limitsDraft)))
      limitsDraft = { ...savedLimits }
    } catch (err) {
      limitsError = failureText(err, 'Failed to update the limits')
    } finally {
      limitsSaving = false
    }
  }
</script>

<SystemSettingsPage title="Explorer and limits" {apply} {error}>
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
        aria-label="Connections view"
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
          aria-label="Let users switch between list and tree"
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

  {#if limitsDraft}
    <section class="card">
      <h2 class="section-title">Limits</h2>
      <p class="hint section-hint">
        What the server takes on at once, so a few big folders or a script can't hold every worker. A request
        over a limit is answered "busy, try again in a few seconds" instead of waiting.
      </p>
      {#each LIMIT_FIELDS as field (field.key)}
        <div class="setting-row column">
          <div>
            <div class="setting-name">{field.label}</div>
            <p class="hint">{field.hint}</p>
          </div>
          <div class="interval-row">
            <input
              class="input interval-input"
              type="number"
              min={field.min}
              max={field.max}
              step="1"
              aria-label={field.label}
              bind:value={limitsDraft[field.key]}
            />
            <span class="hint">{field.unit}</span>
          </div>
        </div>
      {/each}
      <div class="setting-row column">
        {#if limitsError}
          <p class="error">{limitsError}</p>
        {/if}
        <button type="button" class="btn btn-ghost" on:click={saveLimits} disabled={limitsSaving || !limitsDirty}>
          {limitsSaving ? 'Saving…' : 'Save limits'}
        </button>
      </div>
    </section>
  {/if}
</SystemSettingsPage>
