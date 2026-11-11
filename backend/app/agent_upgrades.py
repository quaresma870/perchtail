"""Push-agent upgrades from the portal (#319): where the bundled agent
binaries are, every upgrade attempt, and the automatic upgrades.

The server only tells an agent which version to upgrade to (its own
APP_VERSION). The agent downloads the binary itself -- from this server's
/agent/binaries/ (app/api/agent_ws.py), or from the GitHub release -- and
installs it only if the release key's signature over SHA256SUMS verifies, the
binary matches its line, and the version is newer than its own
(agent/internal/agent/upgrade.go). So nothing here has to be trusted by the
agent, and a compromised server can't make it run anything else.

Every attempt is a row in agent_upgrade_run, from the request to how it
ended: the agent's answer and progress update it, and it only succeeds once
the agent reconnects reporting the new version; with no news from the agent
for TIMEOUT_SECONDS it ends as `timeout`. Failed, refused and timed-out
attempts add to the source's consecutive failure count, a success clears it;
at the failure limit (Settings -> Agents) automatic upgrades skip the source
until an admin resumes it. Each attempt is audited as `agent.upgrade` when
the agent answers the request and again when it ends.

Automatic upgrades (run_scheduled_upgrades, every minute): inside the
window, connected outdated agents whose policy is "automatic" and that
aren't suspended, a few at a time, never one that already failed in the
same window. See app/agent_upgrade_policy.py for the policies and the
window."""

import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlmodel import Session, col, select

from app import agent_upgrade_policy as policies
from app.agent_registry import (
    AgentCommandError,
    AgentError,
    AgentTimeoutError,
    get_agent_registry,
)
from app.audit import record_audit_event
from app.config import get_settings
from app.logging_config import get_logger
from app.models import AgentUpgradeRun, Protocol, Source
from app.timeutils import utcnow
from app.version import APP_VERSION
from app.versions import is_newer, parse_version

logger = get_logger(__name__)

SUMS_FILE = "SHA256SUMS"
SIG_FILE = "SHA256SUMS.sig"

# The agent never pulls more than this from the server (its own cap is the
# same); checked here too so a wrong file in the folder isn't served.
MAX_BINARY_BYTES = 256 * 1024 * 1024

# An upgrade with no news from the agent for this long ends as `timeout`: a
# download that hangs, or an agent that never came back after restarting.
TIMEOUT_SECONDS = 10 * 60

# How long the agent has to accept or refuse; it decides before downloading.
ACCEPT_TIMEOUT_SECONDS = 15.0
# Upgrade requests sent at the same time (each waits for the agent's answer).
SEND_PARALLEL = 16

TOO_OLD = (
    "This agent is too old to upgrade remotely: upgrade it once by hand, "
    "and later versions can be upgraded from here"
)

ACTIVE = ("requested", "accepted", "downloading", "installing", "restarting")
STAGES = ("downloading", "installing", "restarting")
# Attempts that didn't work: not retried in the same window, and counted in
# the window's summary.
UNSUCCESSFUL = ("failed", "timeout", "refused", "too_old")
# Those that add to the consecutive failure count. An agent too old for the
# command isn't failing: it needs one upgrade by hand.
COUNTED_FAILURES = ("failed", "timeout", "refused")


def _binary_name_pattern(version: str) -> re.Pattern[str]:
    return re.compile(
        rf"^perchtail-agent-{re.escape(version)}-[a-z0-9]{{1,16}}-[a-z0-9]{{1,16}}(\.exe)?$"
    )


def binaries_dir() -> Path | None:
    raw = get_settings().agent_binaries_dir.strip()
    return Path(raw) if raw else None


def bundled_binaries() -> tuple[bool, str | None]:
    """Whether this server has the agent binaries of its own version to
    serve, and if not, why. Without them agents fall back to GitHub, which
    needs the agent's host to reach github.com."""
    directory = binaries_dir()
    if directory is None:
        return False, "AGENT_BINARIES_DIR isn't set (an install from source)"
    sums = directory / SUMS_FILE
    if not sums.is_file() or not (directory / SIG_FILE).is_file():
        return False, (
            "No agent binaries are bundled with this server (an image built from source "
            "or in CI); release images carry them"
        )
    try:
        lines = sums.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return False, f"The bundled SHA256SUMS can't be read: {exc}"
    pattern = _binary_name_pattern(APP_VERSION)
    if not any(
        len(parts) == 2 and pattern.match(parts[1].lstrip("*"))
        for parts in (line.split() for line in lines)
    ):
        return False, f"The bundled agent binaries aren't for version {APP_VERSION}"
    return True, None


def binary_path(name: str) -> Path | None:
    """The bundled file `name` -- SHA256SUMS, its signature, or an agent
    binary of this server's version -- or None. Only those exact names are
    ever served, from the one folder. The path returned is one the folder
    listed, picked by comparing names: the requested name never becomes
    part of a path."""
    directory = binaries_dir()
    if directory is None:
        return None
    if name not in (SUMS_FILE, SIG_FILE) and not _binary_name_pattern(APP_VERSION).fullmatch(name):
        return None
    try:
        listed = {entry.name: entry for entry in directory.iterdir()}
    except OSError:
        return None
    path = listed.get(name)
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BINARY_BYTES:
            return None
    except OSError:
        return None
    return path


# --- the attempts ------------------------------------------------------------


class UpgradeInProgressError(Exception):
    pass


# One attempt at a time per source: the check for one in progress and the
# new row are made under this lock (a single process, like the registry).
_begin_lock = threading.Lock()


def _text(value: object, limit: int = 500) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def active_run(session: Session, source_id: int) -> AgentUpgradeRun | None:
    return session.exec(
        select(AgentUpgradeRun)
        .where(AgentUpgradeRun.source_id == source_id, col(AgentUpgradeRun.status).in_(ACTIVE))
        .order_by(col(AgentUpgradeRun.id).desc())
    ).first()


def latest_run(session: Session, source_id: int) -> AgentUpgradeRun | None:
    return session.exec(
        select(AgentUpgradeRun)
        .where(AgentUpgradeRun.source_id == source_id)
        .order_by(col(AgentUpgradeRun.id).desc())
    ).first()


def latest_runs(session: Session, source_ids: list[int]) -> dict[int, AgentUpgradeRun]:
    """Each source's most recent attempt, in one query."""
    if not source_ids:
        return {}
    newest = (
        select(func.max(AgentUpgradeRun.id))
        .where(col(AgentUpgradeRun.source_id).in_(source_ids))
        .group_by(AgentUpgradeRun.source_id)
    )
    rows = session.exec(select(AgentUpgradeRun).where(col(AgentUpgradeRun.id).in_(newest))).all()
    return {run.source_id: run for run in rows}


def delete_runs(session: Session, source_id: int) -> None:
    """With the source: nothing may outlive it (see app.models.NO_ID_REUSE)."""
    session.execute(sa_delete(AgentUpgradeRun).where(AgentUpgradeRun.source_id == source_id))


def not_upgradable(source: Source) -> str | None:
    """Why the source's agent can't be upgraded now, or None. The policy is
    checked by the callers."""
    if source.protocol != Protocol.agent or source.is_system:
        return "Only push-agent sources have an agent to upgrade"
    if not source.enabled:
        return "The source is disabled"
    if not get_agent_registry().is_connected(source.id):
        return "The agent isn't connected"
    if source.agent_version is None:
        # Agents report their version from 0.6 on.
        return TOO_OLD
    if parse_version(source.agent_version) is None:
        return (
            f"The agent reports version {source.agent_version!r}, not a release: "
            "upgrade it by hand"
        )
    if not is_newer(APP_VERSION, source.agent_version):
        return (
            f"The agent runs {source.agent_version}, which isn't older than this "
            f"server ({APP_VERSION})"
        )
    return None


def _audit(session: Session, run: AgentUpgradeRun, result: str, reason: str | None) -> None:
    metadata = {
        "from": run.from_version,
        "to": run.to_version,
        "result": result,
        "trigger": run.trigger,
    }
    if reason:
        metadata["reason"] = reason
    record_audit_event(
        session,
        user_id=run.requested_by_user_id,
        action="agent.upgrade",
        target_type="source",
        target_id=run.source_id,
        metadata=metadata,
    )


def _finish(
    session: Session,
    run: AgentUpgradeRun,
    status: str,
    message: str | None,
    *,
    now: datetime | None = None,
    audit: bool = True,
) -> None:
    now = now or utcnow()
    run.status = status
    run.message = message
    run.finished_at = now
    run.updated_at = now
    source = session.get(Source, run.source_id)
    if source is not None:
        if status == "succeeded":
            source.agent_upgrade_failures = 0
        elif status in COUNTED_FAILURES:
            limit = policies.read_settings(session).failure_limit
            before = source.agent_upgrade_failures or 0
            source.agent_upgrade_failures = before + 1
            if before < limit <= before + 1:
                run.suspended_source = True
                logger.warning(
                    "agent.upgrade_suspended", source_id=run.source_id, failures=before + 1
                )
        session.add(source)
    session.add(run)
    log = logger.info if status == "succeeded" else logger.warning
    log(
        "agent.upgrade_finished",
        source_id=run.source_id,
        from_version=run.from_version,
        to_version=run.to_version,
        trigger=run.trigger,
        result=status,
        reason=message,
    )
    if audit:
        _audit(session, run, status, message)


def _timeout_message(run: AgentUpgradeRun) -> str:
    minutes = TIMEOUT_SECONDS // 60
    if run.status == "restarting":
        return f"The agent didn't come back within {minutes} minutes of restarting"
    return f"The agent didn't come back: no news from it for {minutes} minutes"


def expire_stale(
    session: Session, now: datetime | None = None, *, source_id: int | None = None
) -> int:
    """Ends as `timeout` every attempt with no news from the agent for
    TIMEOUT_SECONDS. Doesn't commit. The number ended."""
    now = now or utcnow()
    query = select(AgentUpgradeRun).where(
        col(AgentUpgradeRun.status).in_(ACTIVE),
        AgentUpgradeRun.updated_at < now - timedelta(seconds=TIMEOUT_SECONDS),
    )
    if source_id is not None:
        query = query.where(AgentUpgradeRun.source_id == source_id)
    runs = session.exec(query).all()
    for run in runs:
        _finish(session, run, "timeout", _timeout_message(run), now=now)
    return len(runs)


def begin_run(
    session: Session,
    source_id: int,
    from_version: str | None,
    *,
    trigger: str,
    user_id: int | None,
    now: datetime | None = None,
) -> AgentUpgradeRun:
    """Stores a new attempt (`requested`) and commits it, or raises
    UpgradeInProgressError when the source has one under way."""
    now = now or utcnow()
    with _begin_lock:
        expire_stale(session, now, source_id=source_id)
        current = active_run(session, source_id)
        if current is not None:
            session.commit()
            raise UpgradeInProgressError(
                f"An upgrade to {current.to_version} is already in progress"
            )
        run = AgentUpgradeRun(
            source_id=source_id,
            command_id=f"upgrade-{uuid.uuid4()}",
            from_version=from_version,
            to_version=APP_VERSION,
            requested_by_user_id=user_id,
            trigger=trigger,
            started_at=now,
            updated_at=now,
            status="requested",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def send_upgrade(source_id: int, command_id: str) -> tuple[str, str | None]:
    """Asks the agent to upgrade and waits for it to accept or refuse:
    ("accepted" | "refused" | "too_old" | "failed", reason). No database,
    so several can run at once."""
    try:
        get_agent_registry().send_command_sync(
            source_id,
            "upgrade",
            timeout=ACCEPT_TIMEOUT_SECONDS,
            command_id=command_id,
            version=APP_VERSION,
        )
    except AgentCommandError as exc:
        if exc.message_type == "unknown_error":
            return "too_old", TOO_OLD
        return "refused", f"The agent refused: {exc}"
    except AgentTimeoutError:
        return "failed", "The agent didn't answer the upgrade request"
    except AgentError as exc:
        return "failed", f"The upgrade request didn't reach the agent: {exc}"
    return "accepted", None


def record_answer(
    session: Session,
    run: AgentUpgradeRun,
    result: str,
    reason: str | None,
    now: datetime | None = None,
) -> None:
    """The agent's answer to the request, audited. Its progress may already
    have moved the attempt on: an acceptance then changes nothing."""
    session.refresh(run)
    _audit(session, run, result, reason)
    if result == "accepted":
        if run.status == "requested":
            run.status = "accepted"
            run.updated_at = now or utcnow()
            session.add(run)
    elif run.status in ACTIVE:
        _finish(session, run, result, reason, now=now, audit=False)


@dataclass
class Started:
    source_id: int
    result: str  # accepted | refused | too_old | failed | busy
    detail: str | None
    run_id: int | None


def start_upgrades(
    session: Session,
    targets: list[tuple[int, str | None]],
    *,
    trigger: str,
    user_id: int | None,
    now: datetime | None = None,
) -> list[Started]:
    """Upgrades the agents of `targets` ((source id, its agent version), all
    checked by the caller): stores each attempt, asks the agents at the same
    time, records and audits their answers, and commits."""
    begun: list[tuple[int, int, str]] = []
    started: dict[int, Started] = {}
    for source_id, from_version in targets:
        try:
            run = begin_run(
                session, source_id, from_version, trigger=trigger, user_id=user_id, now=now
            )
        except UpgradeInProgressError as exc:
            started[source_id] = Started(source_id, "busy", str(exc), None)
            continue
        begun.append((source_id, run.id, run.command_id))
    if len(begun) > 1:
        with ThreadPoolExecutor(max_workers=min(len(begun), SEND_PARALLEL)) as pool:
            answers = list(pool.map(lambda b: send_upgrade(b[0], b[2]), begun))
    else:
        answers = [send_upgrade(source_id, command_id) for source_id, _r, command_id in begun]
    for (source_id, run_id, _c), (result, reason) in zip(begun, answers, strict=True):
        run = session.get(AgentUpgradeRun, run_id)
        if run is not None:
            record_answer(session, run, result, reason, now=now)
        started[source_id] = Started(source_id, result, reason, run_id)
    session.commit()
    return [started[source_id] for source_id, _v in targets if source_id in started]


def handle_message(
    session: Session, source_id: int, message: dict, now: datetime | None = None
) -> bool:
    """An `upgrade_progress` or `upgrade_failed` from the agent. Doesn't
    commit. True if it was about one of the source's attempts under way."""
    command_id = _text(message.get("id"), 100)
    if command_id is None:
        return False
    run = session.exec(
        select(AgentUpgradeRun).where(AgentUpgradeRun.command_id == command_id)
    ).first()
    if run is None or run.source_id != source_id or run.status not in ACTIVE:
        return False
    now = now or utcnow()
    run.updated_at = now
    if message.get("type") == "upgrade_failed":
        _finish(
            session, run, "failed", _text(message.get("error")) or "The upgrade failed", now=now
        )
        return True
    stage = _text(message.get("stage"))
    if stage in STAGES:
        run.status = stage
    elif run.status == "requested":
        run.status = "accepted"
    run.message = _text(message.get("detail"))
    session.add(run)
    logger.info("agent.upgrade_progress", source_id=source_id, stage=stage, detail=run.message)
    return True


def agent_connected(
    session: Session, source_id: int, version: str | None, now: datetime | None = None
) -> None:
    """The agent (re)connected: with the target version, its upgrade
    worked. With another while one was under way, it didn't: the agent
    either failed to restart into it (and put the old binary back) or lost
    its connection while downloading. Doesn't commit."""
    now = now or utcnow()
    expire_stale(session, now, source_id=source_id)
    run = active_run(session, source_id)
    if run is None:
        return
    if version == run.to_version:
        _finish(session, run, "succeeded", None, now=now)
    elif run.status == "restarting":
        _finish(
            session,
            run,
            "failed",
            f"The agent came back with version {version or 'unknown'}, not {run.to_version}",
            now=now,
        )
    elif run.status in ("accepted", "downloading", "installing"):
        _finish(
            session,
            run,
            "failed",
            "The agent's connection dropped during the upgrade; it still runs "
            f"{version or 'an unknown version'}",
            now=now,
        )


def _with_session(bind, action, *args) -> None:
    """For the agent's connection: its own short session and commit, and
    never an error that would take the connection down."""
    try:
        with Session(bind) as session:
            action(session, *args)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("agent.upgrade_update_failed", source_id=args[0], error=str(exc))


def on_agent_message(bind, source_id: int, message: dict) -> None:
    _with_session(bind, handle_message, source_id, message)


# --- automatic upgrades ------------------------------------------------------


def _engine():
    from app.db import engine  # deferred, so tests can patch app.db.engine

    return engine


def _too_old_again(run: AgentUpgradeRun | None, source: Source) -> bool:
    """The agent already said it doesn't know the command, and still runs
    the same version: asking again can't work."""
    return run is not None and run.status == "too_old" and run.from_version == source.agent_version


def run_scheduled_upgrades(now: datetime | None = None, engine=None) -> int:
    """The scheduled job (every minute): ends attempts that timed out, then,
    inside the window, upgrades connected outdated agents whose effective
    policy is "automatic" and that aren't suspended, keeping at most the
    concurrency setting under way, and never asking again one whose attempt
    didn't work in this window. The number of upgrades asked for."""
    now = now or utcnow()
    with Session(engine or _engine()) as session:
        if expire_stale(session, now):
            session.commit()
        settings = policies.read_settings(session)
        zone, _name = policies.server_timezone()
        bounds = policies.window_bounds(settings.window, now, zone)
        if bounds is None:
            return 0
        start, end = bounds
        sources = session.exec(
            select(Source).where(
                Source.protocol == Protocol.agent,
                Source.is_system == False,  # noqa: E712 - SQL comparison
                Source.enabled == True,  # noqa: E712
            )
        ).all()
        automatic = [
            s
            for s in sources
            if policies.effective_policy(s.agent_upgrade_policy, settings.policy) == "automatic"
        ]
        if not automatic:
            return 0
        if policies.remember_window(session, start, end):
            logger.info("agent.upgrade_window_opened", start=start.isoformat(), end=end.isoformat())
            session.commit()
        running = session.exec(
            select(func.count())
            .select_from(AgentUpgradeRun)
            .where(
                col(AgentUpgradeRun.status).in_(ACTIVE),
                AgentUpgradeRun.trigger == "automatic",
            )
        ).one()
        slots = settings.concurrency - running
        if slots <= 0:
            return 0
        tried = set(
            session.exec(
                select(AgentUpgradeRun.source_id).where(
                    AgentUpgradeRun.started_at >= start,
                    col(AgentUpgradeRun.status).in_(UNSUCCESSFUL),
                )
            ).all()
        )
        candidates = [
            s
            for s in automatic
            if s.id not in tried
            and not policies.is_suspended(s.agent_upgrade_failures or 0, settings.failure_limit)
            and not_upgradable(s) is None
        ]
        last = latest_runs(session, [s.id for s in candidates])
        targets = [
            (s.id, s.agent_version)
            for s in candidates
            if not _too_old_again(last.get(s.id), s)
            and (last.get(s.id) is None or last[s.id].status not in ACTIVE)
        ][:slots]
        if not targets:
            return 0
        started = start_upgrades(session, targets, trigger="automatic", user_id=None, now=now)
        logger.info(
            "agent.upgrade_scheduled",
            asked=len(targets),
            accepted=sum(1 for s in started if s.result == "accepted"),
        )
        return len(targets)


def window_summary(session: Session) -> dict | None:
    """The last automatic window: its bounds, and what the attempts the
    schedule started in it came to."""
    bounds = policies.last_window(session)
    if bounds is None:
        return None
    start, end = bounds
    runs = session.exec(
        select(AgentUpgradeRun).where(
            AgentUpgradeRun.trigger == "automatic",
            AgentUpgradeRun.started_at >= start,
            AgentUpgradeRun.started_at < end,
        )
    ).all()
    return {
        "start": start,
        "end": end,
        "upgraded": sum(1 for r in runs if r.status == "succeeded"),
        "failed": sum(1 for r in runs if r.status in UNSUCCESSFUL),
        "suspended": sum(1 for r in runs if r.suspended_source),
        "running": sum(1 for r in runs if r.status in ACTIVE),
    }
