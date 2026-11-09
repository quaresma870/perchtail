# Monitoring PerchTail

PerchTail has four health endpoints, for different consumers:

| Endpoint | Auth | Consumer | What it's for |
|---|---|---|---|
| `GET /healthz` | none | Docker healthcheck, liveness probe | "Is the process up and answering HTTP?" Fast, touches nothing else, always `{"status": "ok"}`. |
| `GET /readyz` | none | Readiness probe, load balancer | "Can this instance take traffic?" A database write, `DATA_DIR` writable, the scheduler running. |
| `GET /monitoring/health` | bearer token | Zabbix and other JSON pollers | The full operational report, worth polling and alerting on. |
| `GET /metrics` | bearer token | Prometheus | The same report in the Prometheus text format. |

Keep `/healthz` as the liveness probe and `/readyz` as the readiness probe: a locked database or a full disk should take the instance out of rotation, not get the container restarted in a loop. The Dockerfile's `HEALTHCHECK` uses `/healthz`; the Kubernetes example in [high-availability.md](high-availability.md) uses both.

## GET /readyz

Answers `200 {"status": "ready"}` when all three checks pass, and `503 {"status": "not_ready", "failed": [...]}` naming the ones that didn't:

- `database`: an insert into a small table, rolled back at once, so it needs SQLite's write lock and leaves nothing behind. It waits at most 2 seconds for the lock (instead of the normal 30), so a probe on a locked database fails fast.
- `data_dir`: a small temporary file is created, written and removed in `DATA_DIR`. A full disk or a read-only mount fails it.
- `scheduler`: the background job scheduler runs.

It needs no token and returns nothing else, so it can be reachable by the orchestrator only.

## Generating a monitoring token

`/monitoring/health` and `/metrics` need their own credential, separate from user sessions: a monitoring system can't do an interactive login. As a super admin (or any role with the `manage_system_settings` global capability), go to **Settings → System** and click **Generate token** under "Monitoring".

The token is shown once, right after it's generated; only its SHA-256 hash is stored (the same pattern as the push-agent token). Copy it somewhere safe; if you lose it, **Regenerate token**, which invalidates the previous one.

Send it as a bearer token:

```bash
curl -H "Authorization: Bearer <token>" https://your-perchtail-host/monitoring/health
```

- Missing or malformed header → `401`
- Wrong token → `401`
- No token has ever been generated for this deployment → `503`

## Response shape

```json
{
  "status": "ok",
  "version": "0.7.0",
  "uptime_seconds": 7704.2,
  "instance": { "hostname": "perchtail-7d9c", "pid": 1 },
  "database": { "reachable": true, "latency_ms": 0.35, "size_bytes": 52428800, "wal_bytes": 4120032 },
  "scratch": { "used_bytes": 0, "max_bytes": 5368709120, "used_fraction": 0.0, "min_free_bytes": 5254463078 },
  "limits": {
    "global_in_use": 3,
    "global_max": 16,
    "per_source_max": 4,
    "busiest_source_in_use": 2,
    "sources_in_use": { "4": 2, "9": 1 },
    "per_user_transfers_max": 2,
    "transfers_in_use": 1,
    "busiest_user_transfers": 1,
    "rejected": { "global": 0, "source": 4, "user": 1 }
  },
  "disks": {
    "data": { "free_bytes": 41234567168, "total_bytes": 105089261568, "free_fraction": 0.39 },
    "scratch": { "free_bytes": 41234567168, "total_bytes": 105089261568, "free_fraction": 0.39 },
    "logs": { "free_bytes": 41234567168, "total_bytes": 105089261568, "free_fraction": 0.39 }
  },
  "sources_by_protocol": { "ssh": 2, "smb": 1, "winrm": 0, "local": 1, "agent": 3 },
  "agent": { "configured": 3, "connected": 2 },
  "name_index": {
    "enabled": true,
    "sources": 7,
    "never_indexed": 0,
    "oldest_indexed_at": "2026-11-08T17:02:11.512301",
    "oldest_age_seconds": 512.4,
    "sources_with_message": 1
  },
  "source_checks": { "failing": 1, "backing_off": 0 },
  "alerts": {
    "last_pass_at": "2026-11-08T17:10:00.123456",
    "last_pass_age_seconds": 12.3,
    "overdue": false,
    "files_waiting": 0,
    "pending_deliveries": 0,
    "failed_deliveries": 0
  },
  "scheduler": {
    "running": true,
    "failed_jobs": 0,
    "jobs": [
      {
        "id": "audit_purge",
        "name": "Audit log purge",
        "next_run_at": "2026-11-09T17:00:00+00:00",
        "last_started_at": "2026-11-08T17:00:00.001",
        "last_finished_at": "2026-11-08T17:00:00.214",
        "last_success_at": "2026-11-08T17:00:00.214",
        "last_success_age_seconds": 612.9,
        "last_ok": true,
        "last_error": null,
        "last_duration_ms": 213
      }
    ]
  },
  "audit_integrity": { "status": "ok", "last_checked_at": "2026-11-01T03:00:00.5", "broken_row_id": null },
  "logs": {
    "used_bytes": 734003200,
    "max_total_bytes": 2147483648,
    "retention_days": 30,
    "size_limit_deleted_early": false,
    "size_limit_deleted_files": 0,
    "size_limit_newest_deleted_day": null
  }
}
```

Timestamps are UTC, without a time zone (`next_run_at` has one). Fields that can't be known are `null`.

## Fields

| Field | Meaning |
|---|---|
| `status` | `ok`, `degraded` or `error`, see [When the status degrades](#when-the-status-degrades). |
| `version` | The running PerchTail version. |
| `uptime_seconds` | Seconds since this process started. |
| `instance.hostname`, `instance.pid` | Which process answered, for a setup with more than one. |
| `database.reachable` | `false` when a `SELECT 1` failed. |
| `database.latency_ms` | How long that `SELECT 1` took. |
| `database.size_bytes`, `database.wal_bytes` | Size of the SQLite file and of its write-ahead log (`perchtail.db-wal`; 0 when there is none). `null` for another database. The WAL is checkpointed and cut back every hour and limited to 64 MiB after each checkpoint. |
| `scratch.used_bytes`, `scratch.max_bytes`, `scratch.used_fraction` | What's on disk under `SCRATCH_DIR`, against `SCRATCH_MAX_GB`. |
| `scratch.min_free_bytes` | Free space kept on the scratch filesystem: the larger of `SCRATCH_MIN_FREE_GB` and `SCRATCH_MIN_FREE_PERCENT` of its size. Below it, and when scratch is at `SCRATCH_MAX_GB`, new fetches are refused with HTTP 507. |
| `limits.global_in_use`, `limits.global_max` | Folder listings, file opens, downloads, folder ZIPs and archive listings talking to sources right now, and how many may at once (Settings → System). |
| `limits.per_source_max`, `limits.busiest_source_in_use`, `limits.sources_in_use` | The limit on one source, the most in use on any one source, and the slots in use per source id. |
| `limits.per_user_transfers_max`, `limits.transfers_in_use`, `limits.busiest_user_transfers` | Downloads and folder ZIPs one user may have in progress, how many are in progress in all, and the most any one user has. |
| `limits.rejected` | Requests answered 503 (busy, with `Retry-After`) since this process started, by the limit that was full: `global`, `source` or `user`. A count that keeps growing means the limits are too low for the load, or something is hammering the server. |
| `disks.data`, `disks.scratch`, `disks.logs` | Free and total bytes, and the free share, of the filesystem holding `DATA_DIR`, `SCRATCH_DIR` and `LOG_DIR`. `null` values when the directory can't be read. On one disk the three are the same. |
| `sources_by_protocol` | Enabled sources per protocol. |
| `agent.configured`, `agent.connected` | Enabled `agent` sources, and push agents connected right now. |
| `name_index.enabled` | The file-name index is on (Settings → System). |
| `name_index.sources` | Enabled sources that take part in the index (not left out on their page). |
| `name_index.never_indexed` | Of those, sources not listed yet. |
| `name_index.oldest_indexed_at`, `name_index.oldest_age_seconds` | The oldest last pass among them, whatever its outcome. It grows when the index can't keep up, and for a source whose checks back off (the index skips it meanwhile). |
| `name_index.sources_with_message` | Sources whose last pass failed or stopped short (a folder that couldn't be listed, too deep); the source page says why. |
| `source_checks.failing` | Enabled sources whose last check or operation couldn't reach them. |
| `source_checks.backing_off` | Enabled sources whose background checks are paused after repeated failures. |
| `alerts.last_pass_at`, `alerts.last_pass_age_seconds` | The last live alert pass. |
| `alerts.overdue` | No pass finished in twice the interval set in Settings → System, plus 30 seconds. |
| `alerts.files_waiting` | Files that didn't fit in the last pass (each source gets at most 20 files and 15 seconds per alert per pass) and will be read in the next ones. |
| `alerts.pending_deliveries` | Webhook calls that failed and are being retried with backoff for up to an hour. |
| `alerts.failed_deliveries` | Webhook calls given up on after that, across all alerts (each alert shows its own count and last error on the Alerts page). |
| `scheduler.running` | The background job scheduler runs. |
| `scheduler.failed_jobs` | Jobs whose last run failed. |
| `scheduler.jobs[]` | Each job: `id` and `name`, `next_run_at`, and its stored record: last start, finish and success, `last_success_age_seconds`, `last_ok`, `last_error` (type and message, with URLs and credential-looking values hidden) and `last_duration_ms`. Record fields are `null` until the job has run once. |
| `audit_integrity.status` | The last audit hash-chain check: `ok`, `broken` (a row was changed or deleted outside PerchTail), `race_breaks` (breaks left by the 0.5.x race that a super admin can record on the Audit Log page) or `unknown` (never checked). |
| `audit_integrity.last_checked_at`, `audit_integrity.broken_row_id` | When it was checked, and the first row that failed. |
| `logs.used_bytes`, `logs.max_total_bytes`, `logs.retention_days` | PerchTail's own logs under `LOG_DIR`, against `LOG_MAX_TOTAL_MB` and `LOG_RETENTION_DAYS`. |
| `logs.size_limit_deleted_early`, `logs.size_limit_deleted_files`, `logs.size_limit_newest_deleted_day` | `true` once `LOG_MAX_TOTAL_MB` has deleted archives that `LOG_RETENTION_DAYS` would still have kept, usually because `LOG_LEVEL=DEBUG` was left on: you have less log history than configured. Lower the log level, set `LOG_QUIET_THIRD_PARTY=true`, or give the logs more space. |

The background jobs are `scratch_idle_sweep`, `scratch_size_guard`, `alert_pass`, `name_index_sweep`, `source_checks`, `audit_purge`, `session_purge`, `update_check`, `audit_integrity_check` and `wal_checkpoint`.

## When the status degrades

- `error`: the database is unreachable.
- `degraded`, any of:
  - scratch use is at or above 90% of `SCRATCH_MAX_GB`;
  - the data, scratch or log filesystem has less than 5% free;
  - the scheduler isn't running, or a job has no next run time;
  - a job's last run failed (`scheduler.failed_jobs` above 0); it clears when the job works again;
  - the live alert pass is overdue;
  - the audit hash chain is `broken`;
  - at least one `agent` source is enabled but no agent is connected.
- `ok`: none of the above.

Some things are reported but don't change the status, because they're about the servers PerchTail reads rather than PerchTail itself, or need a threshold that depends on the setup: failing or backing-off sources, the file-name index age and messages, pending or given-up webhook calls, unrecorded race breaks, and logs deleted early. Alert on those separately (the Zabbix template and the Prometheus rules below do).

## Log events worth alerting on

PerchTail logs JSON lines to stdout and to `LOG_DIR` (see the README). If you ship them to a log platform, these events are worth an alert; the `event` field holds the name.

| Event | Level | What happened |
|---|---|---|
| `startup.credential_salt_missing` | critical | The credential salt file is missing while encrypted data exists, so PerchTail refuses to start. Restore the salt from a backup. |
| `startup.credential_salt_regenerated` | critical | The salt was missing and `CREDENTIAL_SALT_ALLOW_REGENERATE` let PerchTail start with a new one: everything encrypted with the old salt can't be read. |
| `startup.insecure_credential_encryption_key` | critical | `CREDENTIAL_ENCRYPTION_KEY` is still the default; PerchTail refuses to start. |
| `audit_integrity.chain_broken` | error | The audit log hash chain is broken: a row was changed or deleted outside PerchTail. |
| `audit_integrity.race_breaks_unrecorded` | warning | Breaks from the 0.5.x race wait to be recorded on the Audit Log page. |
| `job.failed` | error | A background job raised an error (`job_id` names it); also in `scheduler.jobs[]`. |
| `job.record_not_saved` | warning | A job's run couldn't be recorded (usually a locked or full database). |
| `alert.webhook_failed` | warning | An alert's webhook call failed and will be retried. |
| `alert.webhook_given_up` | warning | An alert's webhook call was given up on after an hour of retries. |
| `alert.evaluate_failed` | error | An alert couldn't be evaluated in a pass. |
| `name_index.source_failed` | warning | The file-name index pass of a source failed. |
| `source.status_changed` | warning | A source started failing (`status: failing`); info when it recovers. |
| `source.check.backing_off` | warning | A source failed its background check three times in a row and backs off. |
| `source.check.timed_out` | warning | A background check went past its deadline. |
| `scratch.size_guard_eviction` | warning | Scratch use passed `SCRATCH_MAX_GB` and files were evicted. |
| `scratch.full`, `scratch.disk_low` | warning | A fetch was refused (507): scratch was at `SCRATCH_MAX_GB` with files people have open, or its filesystem was down to the free space it keeps. |
| `limits.busy` | warning | A request was answered 503 because a limit in Settings → System was full (`scope` says which). |
| `archive.zip_stream_failed` | warning | A folder ZIP failed after it had started streaming, so the client got an incomplete download. |
| `scratch.leftovers_not_cleared` | error | `SCRATCH_DIR` is a filesystem root or holds the database or the logs, so leftovers weren't cleared at startup. Fix `SCRATCH_DIR`. |
| `shutdown.jobs_still_running` | warning | A stop didn't wait for a running job within `SHUTDOWN_TIMEOUT_SECONDS`. |
| `shutdown.pool_still_running` | warning | Same for the file-name index or source-check workers. |
| `db.wal_checkpoint` | warning when `busy` | The hourly WAL checkpoint couldn't finish because readers were busy; it's tried again next hour. |
| `readyz.database_not_writable`, `readyz.data_dir_not_writable` | warning | A readiness check failed. |
| `update_check.failed` | warning | The update check couldn't reach GitHub. |
| `sso.login.discovery_failed`, `sso.callback.no_access_role_missing` | error | SSO sign-in can't work: the provider can't be reached, or the no-access role is missing. |

## Zabbix

Two templates ship with PerchTail, one per Zabbix LTS release:

- [docs/examples/zabbix/perchtail_zabbix_7.0.yaml](examples/zabbix/perchtail_zabbix_7.0.yaml) for Zabbix 7.0 LTS
- [docs/examples/zabbix/perchtail_zabbix_8.0.yaml](examples/zabbix/perchtail_zabbix_8.0.yaml) for Zabbix 8.0 LTS

Both define the template "PerchTail by HTTP" (group Templates/Applications) with one HTTP agent item that reads `{$PERCHTAIL.URL}/monitoring/health` with the header `Authorization: Bearer {$PERCHTAIL.TOKEN}`, and dependent items with JSONPath preprocessing for the status, database, scratch, free disk per directory, agents, file-name index, failing and backing-off sources, live alerts, scheduler, failed jobs, audit integrity and logs. Low-level discovery over `scheduler.jobs` creates, per job, its last result, time since its last success and last duration, and a trigger when its last run failed.

Triggers: status error or degraded, database unreachable, health endpoint not answering (no data for `{$PERCHTAIL.NODATA.TIMEOUT}`, which also catches a wrong token), scheduler not running, little free space per directory (two levels), scratch filling up, push agents disconnected, live alert pass overdue, webhook calls failing or given up, file-name index stale or with failing passes, failing sources, audit hash chain broken, unrecorded race breaks, logs deleted early, a restart and a version change.

| Macro | Default | Meaning |
|---|---|---|
| `{$PERCHTAIL.URL}` | `https://perchtail.example.com` | PerchTail's base URL, without a trailing slash. |
| `{$PERCHTAIL.TOKEN}` | (secret, empty) | The monitoring token. |
| `{$PERCHTAIL.INTERVAL}` | `1m` | How often the report is read. |
| `{$PERCHTAIL.NODATA.TIMEOUT}` | `5m` | No report for this long fires "Health endpoint is not answering". |
| `{$PERCHTAIL.DISK.PFREE.MIN.WARN}` / `.CRIT` | `15` / `5` | Free space, in %, for the warning and high triggers. |
| `{$PERCHTAIL.SCRATCH.PUSED.MAX.WARN}` | `85` | Scratch use, in %, for the warning. |
| `{$PERCHTAIL.NAME_INDEX.AGE.MAX}` | `3600` | Seconds the oldest file-name index pass may reach; keep it well above the index interval. |
| `{$PERCHTAIL.SOURCES.FAILING.MAX}` | `0` | Failing sources allowed before the trigger fires. |

### Importing into Zabbix 7.0

1. In PerchTail, generate a monitoring token (Settings → System → Monitoring).
2. In Zabbix, go to **Data collection → Templates**, click **Import**, choose `perchtail_zabbix_7.0.yaml` and import with the default rules.
3. Create or open a host for PerchTail (no interface is needed), and link the template "PerchTail by HTTP".
4. On the host's **Macros** tab, set `{$PERCHTAIL.URL}` and `{$PERCHTAIL.TOKEN}` (keep the type "Secret text"), and change any threshold you want.
5. After a minute, **Monitoring → Latest data** shows the items; the job items appear after the discovery has run once.

### Importing into Zabbix 8.0

The steps are the same as for 7.0, with `perchtail_zabbix_8.0.yaml`: **Data collection → Templates → Import**, link "PerchTail by HTTP" to a host, set the two macros. The 8.0 file uses export version `8.0` and is otherwise the same as the 7.0 one, including the UUIDs, so a template imported into 7.0 is updated in place when you import the 8.0 file after upgrading Zabbix. Neither uses the macros 8.0 removed (`{TRIGGER.KEY}`, `{STATUS}`, `{HOSTNAME}`, `{IPADDRESS}`, `{ACK.*}`, `{PROFILE.*}` and the like). The master item stays a text item in 8.0; 8.0's new JSON item type isn't needed, since dependent items read the fields.

### Building items by hand

To pick fields yourself instead, make one HTTP agent item on `/monitoring/health` with the bearer header, and dependent items with JSONPath preprocessing (`$.status`, `$.database.reachable`, `$.disks.data.free_fraction`, ...; see [Fields](#fields)). Booleans arrive as `true`/`false` text and nullable fields as `null`, so convert or discard them in preprocessing.

## Prometheus

`GET /metrics` serves the same report in the Prometheus text format (0.0.4), built from the same computation as `/monitoring/health`, behind the same bearer token.

| Metric | Labels | Meaning |
|---|---|---|
| `perchtail_build_info` | `version` | Always 1. |
| `perchtail_status` | | 0 ok, 1 degraded, 2 error. |
| `perchtail_uptime_seconds` | | Seconds since the process started. |
| `perchtail_database_reachable`, `perchtail_database_latency_seconds` | | The database check. |
| `perchtail_database_size_bytes`, `perchtail_database_wal_size_bytes` | | SQLite file and WAL sizes. |
| `perchtail_scratch_used_bytes`, `perchtail_scratch_max_bytes`, `perchtail_scratch_min_free_bytes` | | Scratch use and limit, and the free space kept on its filesystem. |
| `perchtail_connector_slots_in_use`, `perchtail_connector_slots_max`, `perchtail_connector_slots_per_source_max`, `perchtail_connector_slots_busiest_source` | | Listings, opens, downloads and ZIPs talking to sources, against the limits. |
| `perchtail_transfers_in_use`, `perchtail_transfers_per_user_max` | | Downloads and folder ZIPs in progress, and the per-user limit. |
| `perchtail_limit_rejections_total` (counter) | `limit` (`global`, `source`, `user`) | Requests answered 503 because a limit was full. |
| `perchtail_log_used_bytes`, `perchtail_log_max_bytes`, `perchtail_log_size_limit_deleted_early` | | PerchTail's own logs. |
| `perchtail_disk_free_bytes`, `perchtail_disk_total_bytes` | `dir` (`data`, `scratch`, `logs`) | Filesystem space per directory. |
| `perchtail_sources_enabled` | `protocol` | Enabled sources. |
| `perchtail_agents_configured`, `perchtail_agents_connected` | | Push agents. |
| `perchtail_name_index_enabled`, `perchtail_name_index_sources`, `perchtail_name_index_never_indexed`, `perchtail_name_index_oldest_age_seconds`, `perchtail_name_index_sources_with_message` | | The file-name index. |
| `perchtail_sources_failing`, `perchtail_sources_backing_off` | | Source checks. |
| `perchtail_alert_pass_age_seconds`, `perchtail_alert_pass_overdue`, `perchtail_alert_files_waiting`, `perchtail_alert_deliveries_pending`, `perchtail_alert_deliveries_failed` | | Live alerts. |
| `perchtail_scheduler_running`, `perchtail_jobs_failed` | | The scheduler. |
| `perchtail_job_last_ok`, `perchtail_job_last_success_timestamp_seconds`, `perchtail_job_last_duration_seconds` | `job` | Each job's last run. |
| `perchtail_audit_integrity_ok`, `perchtail_audit_integrity_broken`, `perchtail_audit_integrity_last_check_timestamp_seconds` | | The audit hash-chain check. |

A value that isn't known (a job that never ran, a directory that can't be read) is left out rather than reported as `NaN`.

Scrape config, with the token in a file readable by Prometheus:

```yaml
scrape_configs:
  - job_name: perchtail
    scheme: https
    metrics_path: /metrics
    scrape_interval: 60s
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/perchtail_token
    static_configs:
      - targets: ["perchtail.example.com"]
```

Example alert rules:

```yaml
groups:
  - name: perchtail
    rules:
      - alert: PerchTailDown
        expr: up{job="perchtail"} == 0
        for: 5m
        labels: { severity: critical }
        annotations:
          summary: "PerchTail's /metrics doesn't answer (down, or a wrong token)"
      - alert: PerchTailError
        expr: perchtail_status == 2
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "PerchTail can't reach its database"
      - alert: PerchTailDegraded
        expr: perchtail_status == 1
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "PerchTail is degraded; see /monitoring/health"
      - alert: PerchTailJobFailed
        expr: perchtail_job_last_ok == 0
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "PerchTail job {{ $labels.job }} failed its last run"
      - alert: PerchTailDiskLow
        expr: perchtail_disk_free_bytes / perchtail_disk_total_bytes < 0.10
        for: 15m
        labels: { severity: warning }
        annotations:
          summary: "Less than 10% free on PerchTail's {{ $labels.dir }} filesystem"
      - alert: PerchTailAuditChainBroken
        expr: perchtail_audit_integrity_broken == 1
        labels: { severity: critical }
        annotations:
          summary: "PerchTail's audit log hash chain is broken"
      - alert: PerchTailAgentsDisconnected
        expr: perchtail_agents_connected < perchtail_agents_configured
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "Some PerchTail push agents are disconnected"
      - alert: PerchTailNameIndexStale
        expr: perchtail_name_index_oldest_age_seconds > 3600
        for: 15m
        labels: { severity: warning }
        annotations:
          summary: "PerchTail's file-name index hasn't listed a source for over an hour"
```

## A note on network exposure

Don't expose `/monitoring/health` or `/metrics` to the public internet without something in front of them. The bearer token is real auth, but allowing only the monitoring system's address at your reverse proxy (nginx `allow`/`deny`) is a reasonable additional layer, not a replacement for it. `/healthz` and `/readyz` need no token and say little, but they're meant for the orchestrator and load balancer only.
