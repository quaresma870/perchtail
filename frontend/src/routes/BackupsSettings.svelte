<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../lib/api'
  import { currentUser } from '../lib/auth'
  import SystemSettingsPage from '../lib/components/SystemSettingsPage.svelte'
  import UpdatesCard from '../lib/components/UpdatesCard.svelte'
  import { formatSize } from '../lib/file-details'
  import { failureText, saveSystemSettings } from '../lib/system-settings-page'
  import type { BackupInfo, SystemSettings } from '../lib/types'

  let saving = false

  // Backups (#334): the scheduled backup's interval, hours (0 = off), and
  // the backups in BACKUP_DIR, listed only -- never downloadable.
  let backupIntervalInput = '0'
  let backupIntervalSaved = 0
  let backupIntervalError = ''
  let backups: BackupInfo[] = []
  let backupsError = ''

  function apply(settings: SystemSettings) {
    backupIntervalSaved = settings.backup_interval_hours
    backupIntervalInput = String(backupIntervalSaved)
  }

  onMount(async () => {
    try {
      backups = await api.get<BackupInfo[]>('/system-settings/backups')
    } catch (err) {
      backupsError = failureText(err, 'Failed to list the backups')
    }
  })

  async function saveBackupInterval() {
    const parsed = Number(backupIntervalInput)
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 168) {
      backupIntervalError = 'Enter a whole number of hours from 1 to 168, or 0 for off.'
      return
    }
    saving = true
    backupIntervalError = ''
    try {
      apply(await saveSystemSettings({ backup_interval_hours: parsed }))
    } catch (err) {
      backupIntervalError = failureText(err, 'Failed to update the backup interval')
    } finally {
      saving = false
    }
  }
</script>

<SystemSettingsPage title="Backups and updates" {apply}>
  {#if $currentUser?.is_super_admin}
    <UpdatesCard />
  {/if}

  <section class="card">
    <h2 class="section-title">Backups</h2>
    <div class="setting-row column">
      <div>
        <div class="setting-name">Back up every</div>
        <p class="hint">
          An encrypted backup of the database, the credential salt and SSH known_hosts, made while
          PerchTail runs, into the server's backup folder. The last few are kept. From 1 hour to a
          week; 0 turns it off. Backups are made, checked and restored on the server with
          <code>python -m app.backup</code>, never downloaded from here.
        </p>
      </div>
      {#if backupIntervalError}
        <p class="error">{backupIntervalError}</p>
      {/if}
      <div class="interval-row">
        <input
          class="input interval-input"
          type="number"
          min="0"
          max="168"
          step="1"
          aria-label="Backup interval in hours"
          bind:value={backupIntervalInput}
        />
        <span class="hint">hours</span>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={saveBackupInterval}
          disabled={saving || Number(backupIntervalInput) === backupIntervalSaved}
        >
          Save
        </button>
      </div>
    </div>

    <div class="setting-row column">
      <div class="setting-name">Backups on the server</div>
      {#if backupsError}
        <p class="error">{backupsError}</p>
      {:else if backups.length === 0}
        <p class="hint">No backups yet.</p>
      {:else}
        <table class="backup-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Date</th>
              <th scope="col">Size</th>
              <th scope="col">Reason</th>
            </tr>
          </thead>
          <tbody>
            {#each backups as item (item.name)}
              <tr>
                <td class="backup-name">{item.name}</td>
                <td>{new Date(item.created_at).toLocaleString()}</td>
                <td>{formatSize(item.size_bytes)}</td>
                <td>{item.reason}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  </section>
</SystemSettingsPage>

<style>
  .backup-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
    color: var(--text);
  }
  .backup-table th {
    text-align: left;
    font-weight: 600;
    color: var(--text-muted);
    padding: 0.3rem 0.5rem 0.3rem 0;
    border-bottom: 1px solid var(--border-soft);
  }
  .backup-table td {
    padding: 0.3rem 0.5rem 0.3rem 0;
    border-bottom: 1px solid var(--border-soft);
    vertical-align: top;
  }
  .backup-name {
    font-family: var(--font-mono);
    word-break: break-all;
  }
</style>
