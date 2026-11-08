"""GET /metrics (#331): the /monitoring/health report in the Prometheus text
exposition format (version 0.0.4), written by hand -- a few dozen gauges
don't need a client library. Built only from the DetailedHealth that
app.api.monitoring.compute_health returns, so the two never disagree."""

from datetime import UTC, datetime

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# perchtail_status values.
STATUS_CODES = {"ok": 0, "degraded": 1, "error": 2}


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _timestamp(iso: str | None) -> float | None:
    """Seconds since the epoch for a stored naive-UTC ISO timestamp."""
    if iso is None:
        return None
    moment = datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def gauge(self, name: str, help_text: str, samples) -> None:
        """`samples` is a value, or a list of (labels dict, value). A None
        value is left out: Prometheus has no null."""
        if not isinstance(samples, list):
            samples = [({}, samples)]
        samples = [(labels, value) for labels, value in samples if value is not None]
        if not samples:
            return
        self.lines.append(f"# HELP perchtail_{name} {help_text}")
        self.lines.append(f"# TYPE perchtail_{name} gauge")
        for labels, value in samples:
            label_text = ""
            if labels:
                label_text = "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"
            self.lines.append(f"perchtail_{name}{label_text} {_number(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render_metrics(health) -> str:
    out = _Writer()
    out.gauge("build_info", "Always 1; the version is a label.", [({"version": health.version}, 1)])
    out.gauge(
        "status",
        "Overall status: 0 ok, 1 degraded, 2 error.",
        STATUS_CODES.get(health.status, 2),
    )
    out.gauge("uptime_seconds", "Seconds since this process started.", health.uptime_seconds)

    db = health.database
    out.gauge("database_reachable", "1 when the database answered.", db.reachable)
    out.gauge(
        "database_latency_seconds",
        "Time a SELECT 1 took.",
        db.latency_ms / 1000 if db.latency_ms is not None else None,
    )
    out.gauge("database_size_bytes", "Size of the SQLite database file.", db.size_bytes)
    out.gauge("database_wal_size_bytes", "Size of the SQLite write-ahead log.", db.wal_bytes)

    out.gauge("scratch_used_bytes", "Bytes used under SCRATCH_DIR.", health.scratch.used_bytes)
    out.gauge("scratch_max_bytes", "SCRATCH_MAX_GB in bytes.", health.scratch.max_bytes)
    out.gauge("log_used_bytes", "Bytes used by PerchTail's own logs.", health.logs.used_bytes)
    out.gauge("log_max_bytes", "LOG_MAX_TOTAL_MB in bytes.", health.logs.max_total_bytes)
    out.gauge(
        "disk_free_bytes",
        "Free bytes on the filesystem holding each directory.",
        [({"dir": name}, disk.free_bytes) for name, disk in health.disks.items()],
    )
    out.gauge(
        "disk_total_bytes",
        "Size of the filesystem holding each directory.",
        [({"dir": name}, disk.total_bytes) for name, disk in health.disks.items()],
    )

    out.gauge(
        "sources_enabled",
        "Enabled sources by protocol.",
        [({"protocol": p}, n) for p, n in health.sources_by_protocol.items()],
    )
    out.gauge("agents_configured", "Enabled agent sources.", health.agent.configured)
    out.gauge("agents_connected", "Connected push agents.", health.agent.connected)

    index = health.name_index
    out.gauge("name_index_enabled", "1 when the file-name index is on.", index.enabled)
    out.gauge("name_index_sources", "Sources taking part in the file-name index.", index.sources)
    out.gauge(
        "name_index_never_indexed", "Sources not listed by the index yet.", index.never_indexed
    )
    out.gauge(
        "name_index_oldest_age_seconds",
        "Age of the oldest file-name index pass of any source.",
        index.oldest_age_seconds,
    )
    out.gauge(
        "name_index_sources_with_message",
        "Sources whose last index pass failed or stopped short.",
        index.sources_with_message,
    )
    out.gauge("sources_failing", "Enabled sources whose check fails.", health.source_checks.failing)
    out.gauge(
        "sources_backing_off",
        "Enabled sources whose background checks are backing off.",
        health.source_checks.backing_off,
    )

    alerts = health.alerts
    out.gauge(
        "alert_pass_age_seconds",
        "Seconds since the last live alert pass.",
        alerts.last_pass_age_seconds,
    )
    out.gauge("alert_pass_overdue", "1 when the live alert pass is overdue.", alerts.overdue)
    out.gauge("alert_files_waiting", "Files waiting for a later alert pass.", alerts.files_waiting)
    out.gauge(
        "alert_deliveries_pending",
        "Webhook calls waiting to be retried.",
        alerts.pending_deliveries,
    )
    out.gauge(
        "alert_deliveries_failed",
        "Webhook calls given up on, across all alerts.",
        alerts.failed_deliveries,
    )

    sched = health.scheduler
    out.gauge("scheduler_running", "1 when the job scheduler runs.", sched.running)
    out.gauge("jobs_failed", "Background jobs whose last run failed.", sched.failed_jobs)
    out.gauge(
        "job_last_ok",
        "1 when the job's last run worked, 0 when it failed.",
        [({"job": job.id}, job.last_ok) for job in sched.jobs],
    )
    out.gauge(
        "job_last_success_timestamp_seconds",
        "When the job last finished without an error.",
        [({"job": job.id}, _timestamp(job.last_success_at)) for job in sched.jobs],
    )
    out.gauge(
        "job_last_duration_seconds",
        "How long the job's last run took.",
        [
            (
                {"job": job.id},
                job.last_duration_ms / 1000 if job.last_duration_ms is not None else None,
            )
            for job in sched.jobs
        ],
    )

    audit = health.audit_integrity
    out.gauge(
        "audit_integrity_ok",
        "1 when the last audit hash-chain check found no break.",
        audit.status == "ok",
    )
    out.gauge(
        "audit_integrity_broken",
        "1 when the last audit hash-chain check found tampering.",
        audit.status == "broken",
    )
    out.gauge(
        "audit_integrity_last_check_timestamp_seconds",
        "When the audit hash chain was last checked.",
        _timestamp(audit.last_checked_at),
    )
    out.gauge(
        "log_size_limit_deleted_early",
        "1 when LOG_MAX_TOTAL_MB deleted logs LOG_RETENTION_DAYS would have kept.",
        health.logs.size_limit_deleted_early,
    )
    return out.text()
