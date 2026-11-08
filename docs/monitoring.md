# Monitoring PerchTail

PerchTail exposes two health surfaces, aimed at two different consumers. See
[CLAUDE.md](../CLAUDE.md) and [ROADMAP.md](../ROADMAP.md#phase-3) for the
design decisions behind this split.

| Endpoint | Auth | Consumer | What it's for |
|---|---|---|---|
| `GET /healthz` | none | Docker healthcheck / orchestrator | "Is the process up and answering HTTP?" — fast, unauthenticated, no DB round-trip. |
| `GET /monitoring/health` | bearer token | External monitoring (Zabbix, Prometheus, etc.) | Structured operational data worth polling and alerting on. |

This guide covers `GET /monitoring/health`.

## Generating a monitoring token

`GET /monitoring/health` needs its own credential, separate from user
sessions — a monitoring system can't do an interactive cookie-session login.
As a super admin (or any role with the `manage_system_settings` global
capability), go to **Settings → System** and click **Generate token** under
"Monitoring".

The token is shown once, in plaintext, right after it's generated — only its
SHA-256 hash is stored (same pattern as the push-agent enrollment token).
Copy it somewhere safe immediately; if you lose it, generate a new one
(**Regenerate token**), which invalidates the previous one.

Send it as a standard bearer token:

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
  "version": "0.1.1",
  "uptime_seconds": 77.04,
  "database": { "reachable": true, "latency_ms": 0.35 },
  "scratch": { "used_bytes": 0, "max_bytes": 5368709120, "used_fraction": 0.0 },
  "sources_by_protocol": { "ssh": 2, "smb": 1, "winrm": 0, "local": 1, "agent": 3 },
  "agent": { "configured": 3, "connected": 2 },
  "alerts": {
    "last_pass_at": "2026-08-17T17:10:00.123456",
    "overdue": false,
    "files_waiting": 0,
    "pending_deliveries": 0,
    "failed_deliveries": 0
  },
  "scheduler": {
    "running": true,
    "jobs": [
      { "id": "...", "next_run_at": "2026-08-17T17:15:00+00:00" }
    ]
  },
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

`logs.size_limit_deleted_early` turns `true` once `LOG_MAX_TOTAL_MB` has
deleted log archives that `LOG_RETENTION_DAYS` would still have kept,
usually because `LOG_LEVEL=DEBUG` was left on. It doesn't change `status`,
but it means you have less log history than configured; lower the log
level, set `LOG_QUIET_THIRD_PARTY=true`, or give the logs more space.

`alerts` covers live alerts: `files_waiting` counts files that didn't fit in the last pass (each source gets at most 20 files and 15 seconds per alert per pass) and will be read in the next ones; `pending_deliveries` are webhook calls that failed and are being retried with backoff for up to an hour; `failed_deliveries` adds up the calls given up on after that, across all alerts (each alert shows its own count and last error on the Alerts page).

`status` is `"ok"`, `"degraded"`, or `"error"` — a single field to alert on if
you don't want to reason about every sub-metric yourself:

- `error` — the database is unreachable.
- `degraded` — scratch usage is at or above 90% of `scratch_max_gb`, an APScheduler job isn't running or has no next-run time, the live alert pass is overdue (none finished in twice the interval set in Settings → System, plus 30 seconds), or at least one `agent`-protocol source is configured but none are currently connected.
- `ok` — none of the above.

## Zabbix

Modern Zabbix (≥5.0) favors **one HTTP agent item pulling the whole JSON
response, with JSONPath preprocessing per metric**, over one item per
endpoint. Configure a single HTTP agent item against
`https://your-perchtail-host/monitoring/health` with the `Authorization:
Bearer <token>` header, then add dependent items with JSONPath
preprocessing, e.g.:

| Item | JSONPath |
|---|---|
| Overall status | `$.status` |
| DB reachable | `$.database.reachable` |
| DB latency (ms) | `$.database.latency_ms` |
| Scratch used fraction | `$.scratch.used_fraction` |
| Alert pass overdue | `$.alerts.overdue` |
| Alert files waiting for a pass | `$.alerts.files_waiting` |
| Webhook calls waiting to be retried | `$.alerts.pending_deliveries` |
| Webhook calls given up on | `$.alerts.failed_deliveries` |
| Scheduler running | `$.scheduler.running` |
| Connected agents | `$.agent.connected` |
| Logs lost to the size limit | `$.logs.size_limit_deleted_early` |
| Configured agents | `$.agent.connected` vs `$.agent.configured` |

Trigger on `status != "ok"` for a single alert covering everything above, or
on individual dependent items for more granular paging.

## Prometheus

No `/metrics` endpoint exists yet — `/monitoring/health` is the only
monitoring surface today. It's deliberately structured as a thin JSON view
over the same internals a future Prometheus exporter would use, so adding
`GET /metrics` later (a text-exposition-format wrapper reusing the same
health-check logic) shouldn't require reworking anything documented here.

## A note on network exposure

Same rule as everywhere else in this project (see CLAUDE.md's "Security
notes"): don't expose `/monitoring/health` to the public internet without
something in front of it. The bearer token is real auth, but IP-allowlisting
the monitoring system's source address at your reverse proxy (nginx
`allow`/`deny`) is a reasonable *additional* layer, not a replacement for it.
