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
  "search_index": { "last_sweep_at": "2026-08-17T17:10:00.123456", "overdue": false },
  "scheduler": {
    "running": true,
    "jobs": [
      { "id": "...", "next_run_at": "2026-08-17T17:15:00+00:00" }
    ]
  }
}
```

`status` is `"ok"`, `"degraded"`, or `"error"` — a single field to alert on if
you don't want to reason about every sub-metric yourself:

- `error` — the database is unreachable.
- `degraded` — scratch usage is at or above 90% of `scratch_max_gb`, an
  APScheduler job isn't running or has no next-run time, the search-indexing
  sweep is overdue (hasn't completed in over twice
  `search_index_interval_seconds`), or at least one `agent`-protocol source
  is configured but none are currently connected.
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
| Search sweep overdue | `$.search_index.overdue` |
| Scheduler running | `$.scheduler.running` |
| Connected agents | `$.agent.connected` |
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
