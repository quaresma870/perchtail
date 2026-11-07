"""Release check (#215): at most once a day, ask GitHub which release is the
latest, so super admins see under Settings -> System when an upgrade is
available and how to do it.

- Off when UPDATE_CHECK=false (no outbound request, ever) or when the switch
  under Settings -> System is off.
- One unauthenticated request to the public releases API, with a generic
  User-Agent: nothing about this install is sent. The ETag is kept, so a
  check that finds nothing new is a 304 and doesn't count against GitHub's
  limit of 60 requests an hour per IP.
- `releases/latest` skips drafts and pre-releases, so a release candidate is
  never offered.
- HTTPS_PROXY and friends are honoured (trust_env).
- A failure is logged (update_check.failed) and shown as "couldn't check",
  never as an error page.

What comes back is treated as untrusted: the tag must be a PerchTail
version, the link must point at this repository's releases, and the name is
cut to a sane length."""

import re
from datetime import UTC, datetime, timedelta

import httpx
from sqlmodel import Session

from app import system_settings
from app.config import get_settings
from app.logging_config import get_logger
from app.models import UpdateCheckState
from app.timeutils import utcnow
from app.version import APP_VERSION
from app.versions import is_newer, parse_version

logger = get_logger(__name__)

REPOSITORY = "quaresma870/perchtail"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE_PREFIX = f"https://github.com/{REPOSITORY}/releases/"
CHECK_INTERVAL = timedelta(hours=24)
_TIMEOUT_SECONDS = 10.0
_MAX_NAME = 120
STATE_ID = 1
# user:password@ in a URL, as in an HTTPS_PROXY with credentials, which an
# httpx error message can quote.
_URL_CREDENTIALS = re.compile(r"//[^/\s@]+@")


def enabled_by_env() -> bool:
    return bool(get_settings().update_check)


def is_enabled(session: Session) -> bool:
    """UPDATE_CHECK=false always wins; otherwise the System switch decides."""
    return enabled_by_env() and system_settings.get_bool(session, "update_check_enabled")


def install_method() -> str:
    """ "docker" for the official image (it sets PERCHTAIL_INSTALL=docker),
    "source" otherwise."""
    return "docker" if get_settings().perchtail_install.strip().lower() == "docker" else "source"


def get_state(session: Session) -> UpdateCheckState:
    """The singleton row. The migration creates it (id 1), so the job and a
    "Check now" can't race to insert two; created here only for a database
    built without migrations (the tests)."""
    state = session.get(UpdateCheckState, STATE_ID)
    if state is None:
        state = UpdateCheckState(id=STATE_ID)
        session.add(state)
    return state


def update_available(state: UpdateCheckState) -> bool:
    return is_newer(state.latest_version, APP_VERSION)


def _published_at(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _apply_release(state: UpdateCheckState, release) -> None:
    if not isinstance(release, dict):
        raise ValueError("unexpected answer from GitHub (not a release object)")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise ValueError(f"unexpected tag in the latest release: {str(tag)[:40]!r}")
    url = release.get("html_url")
    name = release.get("name")
    state.latest_version = tag.removeprefix("v")
    state.latest_url = (
        url if isinstance(url, str) and url.startswith(RELEASES_PAGE_PREFIX) else None
    )
    state.latest_name = name[:_MAX_NAME] if isinstance(name, str) else None
    state.latest_published_at = _published_at(release.get("published_at"))


def check_now(session: Session, transport: httpx.BaseTransport | None = None) -> UpdateCheckState:
    """Asks GitHub for the latest release and records the outcome on the
    singleton UpdateCheckState. Never raises for a network or GitHub
    problem: that's stored in last_error. Does not commit. `transport` is
    for tests."""
    state = get_state(session)
    headers = {"User-Agent": "PerchTail", "Accept": "application/vnd.github+json"}
    if state.etag and state.latest_version:
        headers["If-None-Match"] = state.etag
    now = utcnow()
    state.last_checked_at = now
    try:
        with httpx.Client(
            timeout=_TIMEOUT_SECONDS, trust_env=True, transport=transport, follow_redirects=False
        ) as client:
            response = client.get(LATEST_RELEASE_URL, headers=headers)
        if response.status_code == 200:
            _apply_release(state, response.json())
            state.etag = response.headers.get("ETag")
        elif response.status_code != 304:
            raise ValueError(f"GitHub answered {response.status_code}")
        state.last_success_at = now
        state.last_error = None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        message = _URL_CREDENTIALS.sub("//***@", str(exc))
        state.last_error = message[:200] or type(exc).__name__
        logger.warning("update_check.failed", error=state.last_error)
        return state

    if update_available(state) and state.notified_version != state.latest_version:
        logger.info("update_check.available", current=APP_VERSION, latest=state.latest_version)
        state.notified_version = state.latest_version
    return state


def run_update_check() -> None:
    """The APScheduler job (app/main.py): runs hourly, checks when on and the
    last check is over a day old, so a restart doesn't add requests."""
    from app.db import engine  # deferred, so tests can patch app.db.engine

    with Session(engine) as session:
        if not is_enabled(session):
            return
        state = get_state(session)
        if state.last_checked_at is not None and utcnow() - state.last_checked_at < CHECK_INTERVAL:
            return
        check_now(session)
        session.commit()
