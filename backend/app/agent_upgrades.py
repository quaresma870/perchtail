"""Push-agent upgrades from the portal (#319): where the bundled agent
binaries are, and the state of each source's upgrade.

The server only tells an agent which version to upgrade to (its own
APP_VERSION). The agent downloads the binary itself -- from this server's
/agent/binaries/ (app/api/agent_ws.py), or from the GitHub release -- and
installs it only if the release key's signature over SHA256SUMS verifies, the
binary matches its line, and the version is newer than its own
(agent/internal/agent/upgrade.go). So nothing here has to be trusted by the
agent, and a compromised server can't make it run anything else.

The upgrade state is in memory, like app/agent_registry.py's connections: an
upgrade in progress is tied to a live connection and a restart of the
server forgets it. Every attempt is audited as `agent.upgrade` when it is
asked for (accepted or refused) and again when it ends (succeeded or
failed)."""

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.logging_config import get_logger
from app.timeutils import utcnow
from app.version import APP_VERSION

logger = get_logger(__name__)

SUMS_FILE = "SHA256SUMS"
SIG_FILE = "SHA256SUMS.sig"

# The agent never pulls more than this from the server (its own cap is the
# same); checked here too so a wrong file in the folder isn't served.
MAX_BINARY_BYTES = 256 * 1024 * 1024

# An upgrade with no news from the agent for this long is given up on:
# a download that hangs, or an agent that never came back after restarting.
STALL_SECONDS = 10 * 60

TOO_OLD = (
    "This agent is too old to upgrade remotely: upgrade it once by hand, "
    "and later versions can be upgraded from here"
)

UpgradeStatus = Literal["requested", "running", "restarting", "succeeded", "failed"]
_ACTIVE = ("requested", "running", "restarting")


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


@dataclass
class UpgradeState:
    source_id: int
    user_id: int | None
    command_id: str
    from_version: str | None
    to_version: str
    status: UpgradeStatus = "requested"
    stage: str | None = None
    detail: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    last_news: float = field(default_factory=time.monotonic)

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE


class UpgradeInProgressError(Exception):
    pass


AuditWriter = Callable[[UpgradeState], None]


def _write_final_audit(state: UpgradeState) -> None:
    """How an accepted upgrade ended, in its own session: it's learned from
    the agent's connection, outside any request."""
    from sqlmodel import Session

    from app.audit import record_audit_event
    from app.db import engine  # deferred, like app/source_status.py

    try:
        with Session(engine) as session:
            record_audit_event(
                session,
                user_id=state.user_id,
                action="agent.upgrade",
                target_type="source",
                target_id=state.source_id,
                metadata=final_audit_metadata(state),
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - never take the agent's connection down
        logger.error("agent.upgrade_audit_failed", source_id=state.source_id, error=str(exc))


def final_audit_metadata(state: UpgradeState) -> dict:
    metadata = {
        "from": state.from_version,
        "to": state.to_version,
        "result": state.status,
    }
    if state.status == "failed" and state.detail:
        metadata["reason"] = state.detail
    return metadata


class AgentUpgrades:
    """Each source's latest upgrade. Thread-safe: the endpoints run in the
    thread pool, the agent's messages arrive on the event loop."""

    def __init__(self, audit_writer: AuditWriter | None = None):
        self._states: dict[int, UpgradeState] = {}
        self._lock = threading.Lock()
        self._audit_writer = audit_writer or _write_final_audit
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-upgrade")

    def set_audit_writer(self, writer: AuditWriter | None) -> None:
        self._audit_writer = writer or _write_final_audit

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    def _expire(self, state: UpgradeState) -> bool:
        """Under the lock: gives up on an upgrade with no news for too long."""
        if state.active and time.monotonic() - state.last_news > STALL_SECONDS:
            minutes = STALL_SECONDS // 60
            if state.status == "restarting":
                reason = f"The agent didn't come back within {minutes} minutes of restarting"
            else:
                reason = f"No news from the agent for {minutes} minutes"
            self._finish(state, "failed", reason)
            return True
        return False

    def get(self, source_id: int) -> UpgradeState | None:
        with self._lock:
            state = self._states.get(source_id)
            if state is not None:
                self._expire(state)
            return state

    def begin(
        self,
        source_id: int,
        *,
        user_id: int | None,
        command_id: str,
        from_version: str | None,
        to_version: str,
    ) -> UpgradeState:
        with self._lock:
            current = self._states.get(source_id)
            if current is not None and not self._expire(current) and current.active:
                raise UpgradeInProgressError(
                    f"An upgrade to {current.to_version} is already in progress"
                )
            state = UpgradeState(
                source_id=source_id,
                user_id=user_id,
                command_id=command_id,
                from_version=from_version,
                to_version=to_version,
            )
            self._states[source_id] = state
            return state

    def accepted(self, source_id: int, command_id: str) -> None:
        with self._lock:
            state = self._current(source_id, command_id)
            if state is not None and state.status == "requested":
                state.status = "running"
                state.last_news = time.monotonic()

    def refused(self, source_id: int, command_id: str, reason: str) -> None:
        """The agent (or its connection) refused before starting: audited by
        the endpoint along with the request, so not again here."""
        with self._lock:
            state = self._current(source_id, command_id)
            if state is not None and state.active:
                self._finish(state, "failed", reason, audit=False)

    def handle_message(self, source_id: int, message: dict) -> bool:
        """An `upgrade_progress` or `upgrade_failed` from the agent. True if
        it was about the source's current upgrade."""
        command_id = str(message.get("id", ""))
        with self._lock:
            state = self._current(source_id, command_id)
            if state is None or not state.active:
                return False
            state.last_news = time.monotonic()
            if message.get("type") == "upgrade_failed":
                self._finish(state, "failed", _text(message.get("error")) or "The upgrade failed")
                return True
            stage = _text(message.get("stage"))
            state.stage = stage
            state.detail = _text(message.get("detail"))
            state.status = "restarting" if stage == "restarting" else "running"
            logger.info(
                "agent.upgrade_progress", source_id=source_id, stage=stage, detail=state.detail
            )
            return True

    def agent_connected(self, source_id: int, version: str | None) -> None:
        """The agent (re)connected: with the target version, its upgrade
        worked. With another while one was under way, the upgrade didn't
        happen: the agent either failed to restart into it (and put the old
        binary back) or lost its connection while downloading."""
        with self._lock:
            state = self._states.get(source_id)
            if state is None or not state.active or self._expire(state):
                return
            if version == state.to_version:
                self._finish(state, "succeeded", None)
            elif state.status == "restarting":
                self._finish(
                    state,
                    "failed",
                    f"The agent came back with version {version or 'unknown'}, "
                    f"not {state.to_version}",
                )
            elif state.status == "running":
                self._finish(
                    state,
                    "failed",
                    "The agent's connection dropped during the upgrade; it still runs "
                    f"{version or 'an unknown version'}",
                )

    def _current(self, source_id: int, command_id: str) -> UpgradeState | None:
        state = self._states.get(source_id)
        if state is None or state.command_id != command_id:
            return None
        return state

    def _finish(
        self, state: UpgradeState, status: UpgradeStatus, detail: str | None, *, audit: bool = True
    ) -> None:
        state.status = status
        state.detail = detail
        state.finished_at = utcnow()
        log = logger.info if status == "succeeded" else logger.warning
        log(
            "agent.upgrade_finished",
            source_id=state.source_id,
            from_version=state.from_version,
            to_version=state.to_version,
            result=status,
            reason=detail,
        )
        if audit:
            snapshot = UpgradeState(**{f: getattr(state, f) for f in state.__dataclass_fields__})
            self._executor.submit(self._audit_writer, snapshot)


def _text(value: object, limit: int = 500) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


@lru_cache
def get_agent_upgrades() -> AgentUpgrades:
    return AgentUpgrades()
