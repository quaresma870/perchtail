"""Phase 3 alerting (see ROADMAP.md) — notifies a webhook when new content
matching a saved query (Alert, see app/models.py) appears in a source's
full-text search index. Rides entirely on the Phase 3 index/indexer
(app/search_index.py): evaluate_alerts() is called at the end of every
run_indexing_sweep(), never on its own schedule, so it always sees that
sweep's freshly-updated SearchIndexState.indexed_at timestamps rather than
racing ahead of or behind indexing."""

import httpx
from sqlmodel import Session, select

from app.auth.models import Capability, GlobalCapability, User
from app.auth.rbac import visible_source_ids
from app.logging_config import get_logger
from app.models import Alert, AlertFileState, SearchIndexState, Source
from app.search_index import SearchHit, last_indexed_line, search_file_content_after
from app.timeutils import utcnow
from app.webhook_safety import UnsafeWebhookURLError, resolve_pinned_webhook_target

logger = get_logger(__name__)

_WEBHOOK_TIMEOUT_SECONDS = 10.0
_MAX_HITS_PER_WEBHOOK = 10
# Bounds memory for a file that suddenly gains a huge number of matching
# lines; matched_count in the webhook stops counting there.
_MAX_HITS_PER_FILE = 500


def _owner_may_alert(owner: User | None) -> bool:
    """Alerts are an export channel, so they only fire while their owner is
    active and still holds manage_alerts, re-checked live like source RBAC."""
    if owner is None or not owner.active:
        return False
    return owner.role.is_super_admin or GlobalCapability.manage_alerts in (
        owner.role.global_capabilities
    )


def _resolve_alert_source_ids(session: Session, owner: User, alert: Alert) -> set[int]:
    """The sources this alert is actually allowed to look at right now --
    RBAC is re-checked here, at evaluation time, not read from anything
    frozen at alert-creation time (see Alert's docstring). Further narrowed
    to sources with search_indexing_enabled, since an alert can only ever
    fire on what the index actually covers."""
    visible = visible_source_ids(session, owner, Capability.view)

    if alert.source_id is not None:
        if visible is not None and alert.source_id not in visible:
            return set()
        candidate_ids = {alert.source_id}
    elif visible is not None:
        candidate_ids = visible
    else:
        candidate_ids = set(session.exec(select(Source.id).where(Source.enabled.is_(True))).all())

    if not candidate_ids:
        return set()

    indexed = session.exec(
        select(Source.id).where(
            Source.id.in_(candidate_ids), Source.search_indexing_enabled.is_(True)
        )
    ).all()
    return set(indexed)


def _changed_files_since(session: Session, source_ids: set[int], since) -> set[tuple[int, str]]:
    query = select(SearchIndexState).where(SearchIndexState.source_id.in_(source_ids))
    if since is not None:
        query = query.where(SearchIndexState.indexed_at > since)
    return {(s.source_id, s.file_path) for s in session.exec(query).all()}


def _new_hits_for_source_ids(
    session: Session, alert: Alert, source_ids: set[int]
) -> tuple[list[SearchHit], dict[tuple[int, str], int]]:
    """Returns (new hits, per-file line each changed file is now reported
    through). Each changed file is searched on its own, past the line this
    alert already reported up to -- the indexer re-reads a changed file in
    full, so without that cut every old match would fire again."""
    if not source_ids:
        return [], {}
    changed = _changed_files_since(session, source_ids, alert.last_checked_at)
    if not changed:
        return [], {}

    reported = {
        (state.source_id, state.file_path): state.reported_through_line
        for state in session.exec(
            select(AlertFileState).where(AlertFileState.alert_id == alert.id)
        ).all()
    }
    hits: list[SearchHit] = []
    watermarks: dict[tuple[int, str], int] = {}
    for source_id, file_path in sorted(changed):
        last_line = last_indexed_line(session, source_id, file_path)
        after_line = reported.get((source_id, file_path), 0)
        if last_line < after_line:
            # The file now ends before where we'd got to: rotated or
            # rewritten, so all of it is new content.
            after_line = 0
        hits.extend(
            search_file_content_after(
                session, alert.query, source_id, file_path, after_line, _MAX_HITS_PER_FILE
            )
        )
        watermarks[(source_id, file_path)] = last_line
    return hits, watermarks


def _record_watermarks(
    session: Session, alert: Alert, watermarks: dict[tuple[int, str], int]
) -> None:
    for (source_id, file_path), line in watermarks.items():
        state = session.get(AlertFileState, (alert.id, source_id, file_path))
        if state is None:
            state = AlertFileState(
                alert_id=alert.id,
                source_id=source_id,
                file_path=file_path,
                reported_through_line=line,
            )
        state.reported_through_line = line
        session.add(state)


def check_alert(session: Session, alert: Alert) -> list[SearchHit]:
    """Returns the new hits this alert should fire for, without sending
    anything -- kept separate from evaluate_alerts()/send_webhook() so the
    matching logic is testable without a live HTTP endpoint. Resolves
    source scope (RBAC + indexing opt-in) itself; evaluate_alerts() below
    resolves it once and reuses _new_hits_for_source_ids directly instead,
    since it also needs the resolved set to decide whether to advance
    last_checked_at."""
    owner = session.get(User, alert.user_id)
    if not _owner_may_alert(owner):
        return []
    source_ids = _resolve_alert_source_ids(session, owner, alert)
    hits, _ = _new_hits_for_source_ids(session, alert, source_ids)
    return hits


def send_webhook(alert: Alert, hits: list[SearchHit]) -> bool:
    payload = {
        "alert_id": alert.id,
        "alert_name": alert.name,
        "query": alert.query,
        "matched_count": len(hits),
        "hits": [
            {
                "source_id": hit.source_id,
                "file_path": hit.file_path,
                "line_number": hit.line_number,
                "snippet_html": hit.snippet_html,
            }
            for hit in hits[:_MAX_HITS_PER_WEBHOOK]
        ],
    }
    try:
        # Resolved and validated here, not just at alert create/update time
        # (see app.api.alerts) -- and pinned to the specific IP that
        # validation just saw, not handed off as a plain hostname for httpx
        # to resolve all over again a moment later. A second call to
        # assert_webhook_url_is_safe() followed by httpx.post(url) would
        # look like it closes the DNS-rebinding gap but doesn't: those would
        # still be two independent resolutions, and an attacker's own
        # authoritative DNS server can answer them differently. See
        # webhook_safety.py's module docstring.
        target = resolve_pinned_webhook_target(alert.webhook_url)
        # The module-level httpx.post() convenience function doesn't accept
        # `extensions` (needed to carry sni_hostname through) -- a Client is
        # required to pass it.
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = client.post(
                target.url,
                json=payload,
                headers={"Host": target.host_header},
                extensions={"sni_hostname": target.sni_hostname},
            )
        response.raise_for_status()
        logger.info("alert.webhook_sent", alert_id=alert.id, matched_count=len(hits))
        return True
    except UnsafeWebhookURLError as exc:
        logger.warning("alert.webhook_blocked", alert_id=alert.id, error=str(exc))
        return False
    except httpx.HTTPError as exc:
        logger.warning("alert.webhook_failed", alert_id=alert.id, error=str(exc))
        return False


def evaluate_alerts() -> None:
    """Called at the end of every search-indexing sweep (see
    search_index.run_indexing_sweep). One alert's failure is logged and
    skipped rather than aborting evaluation for every other alert -- same
    per-item isolation as the indexing sweep itself."""
    from app.db import engine  # deferred: app.db imports search_index for schema setup

    with Session(engine) as session:
        alerts = session.exec(select(Alert).where(Alert.enabled.is_(True))).all()
        for alert in alerts:
            try:
                owner = session.get(User, alert.user_id)
                source_ids = (
                    _resolve_alert_source_ids(session, owner, alert)
                    if _owner_may_alert(owner)
                    else set()
                )
                hits, watermarks = _new_hits_for_source_ids(session, alert, source_ids)
                if hits:
                    send_webhook(alert, hits)
                # Only advance the watermark when this alert actually had
                # something in scope to check -- if it's fully blocked
                # (owner deactivated, RBAC revoked, nothing indexed), leave
                # last_checked_at where it was so a later restore re-checks
                # everything missed in between, instead of silently
                # skipping it.
                if source_ids:
                    _record_watermarks(session, alert, watermarks)
                    alert.last_checked_at = utcnow()
                    session.add(alert)
                    session.commit()
            except Exception:
                logger.exception("alert.evaluate_failed", alert_id=alert.id)
                session.rollback()
