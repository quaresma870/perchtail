import asyncio
import importlib
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app import instance_lock, name_index, source_status
from app.agent_registry import get_agent_registry
from app.agent_upgrades import run_scheduled_upgrades
from app.alerts import run_alert_pass
from app.api.agent_upgrades import router as agent_upgrades_router
from app.api.agent_ws import router as agent_ws_router
from app.api.alerts import router as alerts_router
from app.api.archive import router as archive_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.config_transfer import router as config_transfer_router
from app.api.customers import router as customers_router
from app.api.errors import install as install_error_handlers
from app.api.errors import path_outside_base as _path_outside_base
from app.api.files import router as files_router
from app.api.folders import router as folders_router
from app.api.monitoring import metrics_router
from app.api.monitoring import router as monitoring_router
from app.api.roles import router as roles_router
from app.api.rules import router as rules_router
from app.api.severity_patterns import global_router as severity_patterns_global_router
from app.api.severity_patterns import source_router as severity_patterns_source_router
from app.api.sources import router as sources_router
from app.api.sso import router as sso_router
from app.api.system_settings import router as system_settings_router
from app.api.updates import router as updates_router
from app.api.users import router as users_router
from app.audit_hash_chain import backfill_chain_if_needed
from app.audit_integrity import run_audit_integrity_check
from app.audit_purge import run_audit_purge_sweep
from app.auth.sessions import run_session_purge_sweep
from app.backup import SCHEDULE_TICK as BACKUP_TICK
from app.backup import record_pending_restore, run_scheduled_backup
from app.bootstrap import (
    assert_credential_salt_present,
    backfill_sso_user_issuers,
    seed_initial_super_admin,
    seed_no_access_role,
    seed_severity_patterns,
    seed_system_log_source,
)
from app.collectors.local import LocalPathNotAllowedError
from app.config import get_settings
from app.crypto import audit_chain_key
from app.data_layout import leftover_legacy_database
from app.data_layout import report as report_data_layout
from app.db import engine, init_db
from app.db import register_upkeep_jobs as register_db_upkeep_jobs
from app.health import mark_started, readiness_failures
from app.jobs import register as register_job
from app.jobs import shutdown as shutdown_background_work
from app.logging_config import configure_logging, get_logger
from app.monitoring_access import require_probe_access
from app.name_index import run_name_index_sweep
from app.scheduler import scheduler
from app.schema_version import check_database_revision
from app.scratch import get_scratch_store
from app.source_status import run_source_checks
from app.update_check import run_update_check
from app.version import APP_VERSION

configure_logging()
logger = get_logger(__name__)

# Opt-in-only test-double hook, never set outside
# backend/scripts/run_e2e_server.sh (see app/testing/fake_winrm.py): names a
# module to import once at startup, for wiring in a fake protocol connector
# where no real target is available (real WinRM needs an actual Windows
# host, unlike ssh/smb which the e2e suite points at real local test
# servers). Absent everywhere else -- a normal deployment's .env has no
# reason to ever set this, and this line does nothing unless it's set.
_test_patch_module = os.environ.get("PERCHTAIL_TEST_PATCH_MODULE")
if _test_patch_module:
    logger.warning("startup.test_patch_module_loaded", module=_test_patch_module)
    importlib.import_module(_test_patch_module)

# The default a fresh checkout ships with (see app/config.py) -- anyone can
# derive the encryption key it produces from this project's own public
# source, so starting with it still set would silently encrypt every
# stored credential (SSH keys, SMB/WinRM passwords, SSO client secrets)
# under a key that isn't actually secret at all.
_INSECURE_DEFAULT_CREDENTIAL_KEY = "changeme"


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


# CSP: everything the frontend needs is same-origin (see frontend/index.html --
# no third-party scripts/styles/fonts anywhere). style-src needs
# 'unsafe-inline' because CodeMirror 6 (app/CLAUDE.md's chosen viewer) injects
# its editor styles via a runtime <style> element (the style-mod package), not
# a static stylesheet -- there's no nonce to hand a static SPA build, so this
# is the standard, accepted trade-off for CodeMirror-based apps. script-src
# stays strict ('self' only, no unsafe-inline/unsafe-eval), which is the
# directive that actually matters against XSS. frame-ancestors 'none' is the
# modern equivalent of X-Frame-Options: DENY, kept alongside it for older
# browsers/embedded webviews that don't honor CSP framing directives. HSTS is
# deliberately not set here -- it's a deployment-level concern wherever TLS
# actually terminates, per CLAUDE.md's "sit behind a reverse proxy" note.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies to every response, API and static frontend alike -- see
    ROADMAP.md's security hardening backlog. Cheap and blanket rather than
    tuned per-route: none of these headers restrict anything this app
    actually needs (no inline scripts, no third-party embeds, no framing by
    another origin)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Tells the browser this answer came from PerchTail itself, so a 502
        # about a source isn't mistaken for a proxy saying PerchTail is down
        # (#276, frontend/src/lib/server-status.ts).
        response.headers["X-PerchTail"] = "1"
        return response


_ORIGIN_CHECKED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_same_origin(candidate: str, expected_netloc: str) -> bool:
    """candidate is a raw Origin header value, or a full Referer URL -- both
    get reduced to host[:port] before comparing, since Referer carries a
    path/query Origin never does. Deliberately host-only, not
    scheme+host -- see OriginCheckMiddleware's docstring for why comparing
    scheme would break the standard reverse-proxy deployment this project
    is actually meant to run behind."""
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    return parsed.netloc == expected_netloc


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth CSRF protection layered on top of the session
    cookie's SameSite=strict attribute (app/api/auth.py's
    _set_session_cookie) -- see ROADMAP.md's security hardening backlog.
    SameSite=Strict is already sufficient CSRF protection on its own in any
    standards-compliant browser: it blocks the cookie on every cross-site
    request, including top-level navigation (unlike Lax), so a forged
    cross-site request simply arrives unauthenticated regardless of what
    parameters it carries. This closes the one residual gap that leaves --
    a browser bug or a non-standard client that doesn't honor SameSite.

    For every state-changing request, the Origin header (falling back to
    Referer, since some legitimate same-origin requests omit Origin in edge
    cases) must match *this same request's own Host header* -- deliberately
    self-referential rather than compared against a configured value like
    public_base_url, both because it's the standard approach (OWASP's CSRF
    cheat sheet: compare Origin against the target origin "as registered
    with the server", which in practice means Host) and because it's the
    only thing that works correctly in every deployment shape this project
    actually has: behind a reverse proxy in production (nginx/whatever
    passes Host through) and via Vite's dev-server proxy locally (`npm run
    dev` runs the frontend on its own port, proxying API calls to the
    backend on a different one -- see vite.config.ts, which strips
    Origin/Referer on the way through so this falls to the no-header case
    below rather than comparing two ports that are legitimately different).

    Host-only, deliberately not scheme+host: uvicorn runs with no
    --proxy-headers/--forwarded-allow-ips trust configured (see
    docker-entrypoint.sh), so request.url.scheme is always "http" as seen
    from nginx's plain-HTTP proxy connection, even when the real client
    connected over https and its Origin header correctly says so. Comparing
    scheme too would 403 every state-changing request -- including
    login -- on exactly the TLS-terminating-reverse-proxy setup this
    project's docs recommend (docs/source-setup.md's "reachability" note
    and the README's deployment section). Dropping scheme from the
    comparison doesn't meaningfully weaken this: SameSite=Strict above is
    already the real defense; this only guards a non-compliant client, and
    a same-host, different-scheme request is not a cross-site one in the
    threat model CSRF checks exist for.

    If neither header is present at all, the request is let through to
    rely on SameSite alone -- rejecting on a merely absent header would
    also break legitimate non-browser API use (curl, scripts) that a
    security review shouldn't introduce as a side effect."""

    async def dispatch(self, request: Request, call_next):
        if request.method in _ORIGIN_CHECKED_METHODS:
            candidate = request.headers.get("origin") or request.headers.get("referer")
            if candidate is not None:
                if not _is_same_origin(candidate, request.url.netloc):
                    return JSONResponse(
                        status_code=403, content={"detail": "Cross-site request blocked"}
                    )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.credential_encryption_key == _INSECURE_DEFAULT_CREDENTIAL_KEY:
        logger.critical("startup.insecure_credential_encryption_key")
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is still the insecure default "
            f"({_INSECURE_DEFAULT_CREDENTIAL_KEY!r}) -- refusing to start. Set a "
            "real value in .env before starting (see README's Quick start / "
            ".env.example); every stored credential is encrypted with a key "
            "derived from it, so this can't be silently defaulted."
        )

    # One instance per DATA_DIR (#334): taken before anything moves files or
    # opens the database, held until shutdown has finished.
    try:
        lock = instance_lock.acquire(settings)
    except instance_lock.InstanceLockHeldError as exc:
        instance_lock.log_held(logger, exc)
        raise
    try:
        async with _started(app, settings):
            yield
    finally:
        lock.release()


@asynccontextmanager
async def _started(app: FastAPI, settings):
    report_data_layout(settings)
    # A database migrated by a newer PerchTail (#334): refuse before
    # init_db() touches it.
    check_database_revision(engine, logger)
    leftover = leftover_legacy_database(settings)
    if leftover is not None:
        logger.warning(
            "startup.data_layout.legacy_database_left",
            legacy=str(leftover),
            in_use=settings.database_url,
            hint="a database from before 0.5.0 is still in DATA_DIR's parent; "
            "if it holds your data, stop PerchTail and move it into DATA_DIR",
        )
    init_db()
    mark_started()
    get_agent_registry().bind_loop(asyncio.get_running_loop())
    with Session(engine) as session:
        assert_credential_salt_present(session)
        # Must run before anything below that could write an AuditLog row --
        # see backfill_chain_if_needed's own docstring for why order matters
        # here.
        backfilled = backfill_chain_if_needed(audit_chain_key(), session)
        if backfilled:
            logger.info("audit_hash_chain.backfill_complete", rows=backfilled)
        session.commit()
        # A restore (app/backup.py) can't audit itself in the database it
        # replaces: the first start on the restored database does.
        if record_pending_restore(session, settings):
            session.commit()
        seed_system_log_source(session)
        seed_initial_super_admin(session)
        seed_no_access_role(session)
        seed_severity_patterns(session)
        backfill_sso_user_issuers(session)

    store = get_scratch_store()
    # Nothing in scratch can be in use yet: whatever is there was left by a
    # crash or a killed container (#329).
    db_file = settings.database_url.removeprefix("sqlite:///")
    store.clear_leftovers(
        # Also the salt and known_hosts, which may live outside DATA_DIR:
        # losing the salt makes every stored credential unreadable.
        protected=[
            Path(settings.data_dir),
            Path(settings.log_dir),
            Path(settings.backup_dir),
            Path(db_file).parent,
            Path(settings.credential_salt_path),
            Path(settings.ssh_known_hosts_path),
        ]
    )
    sweep = timedelta(seconds=settings.scratch_sweep_interval_seconds)
    register_job(
        scheduler,
        "scratch_idle_sweep",
        "Scratch idle sweep",
        store.sweep_idle,
        sweep,
        kwargs={"idle_seconds": settings.scratch_idle_seconds},
    )
    register_job(
        scheduler, "scratch_size_guard", "Scratch size guard", store.enforce_size_guard, sweep
    )
    # Live alerts (#305): ticks every 15 s and runs a pass once the interval
    # set in Settings -> System has gone by.
    register_job(scheduler, "alert_pass", "Live alerts", run_alert_pass, timedelta(seconds=15))
    # File-name index (#304): every minute it lists whatever sources are due.
    register_job(
        scheduler, "name_index_sweep", "File-name index", run_name_index_sweep, timedelta(minutes=1)
    )
    # Background source check (#276): every minute it checks whatever is due.
    register_job(
        scheduler, "source_checks", "Source checks", run_source_checks, timedelta(minutes=1)
    )
    # Agent upgrades (#319): every minute it ends attempts that timed out
    # and, inside the window, upgrades the agents set to "automatic".
    register_job(
        scheduler,
        "agent_upgrades",
        "Automatic agent upgrades",
        run_scheduled_upgrades,
        timedelta(minutes=1),
    )
    # The long-interval jobs catch up after a restart (#327): with a fresh
    # interval on every start, a container restarted more often than the
    # interval never ran them.
    register_job(
        scheduler,
        "audit_purge",
        "Audit log purge",
        run_audit_purge_sweep,
        timedelta(seconds=settings.audit_purge_interval_seconds),
        catch_up=True,
    )
    register_job(
        scheduler,
        "session_purge",
        "Expired session purge",
        run_session_purge_sweep,
        timedelta(hours=1),
        catch_up=True,
    )
    register_job(
        scheduler,
        "update_check",
        "Update check",
        run_update_check,
        timedelta(hours=1),
        catch_up=True,
    )
    register_job(
        scheduler,
        "audit_integrity_check",
        "Audit log integrity check",
        run_audit_integrity_check,
        timedelta(days=settings.audit_integrity_check_interval_days),
        catch_up=True,
    )
    # Scheduled backup (#334): ticks every few minutes and backs up once the
    # interval set in Settings -> System has gone by; 0 (the default) is off.
    register_job(scheduler, "backup", "Scheduled backup", run_scheduled_backup, BACKUP_TICK)
    register_db_upkeep_jobs(scheduler)
    scheduler.start()
    logger.info("startup.complete")
    yield
    # Let running jobs and pool work finish, within a limit (#335).
    await asyncio.to_thread(
        shutdown_background_work,
        scheduler,
        [name_index.pool(), source_status.pool()],
        settings.shutdown_timeout_seconds,
    )


app = FastAPI(title="PerchTail", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
app.include_router(alerts_router)
app.include_router(files_router)
app.include_router(archive_router)
app.include_router(audit_router)
app.include_router(config_transfer_router)
app.include_router(customers_router)
app.include_router(folders_router)
app.include_router(sources_router)
app.include_router(rules_router)
app.include_router(roles_router)
app.include_router(users_router)
app.include_router(sso_router)
app.include_router(agent_ws_router)
app.include_router(agent_upgrades_router)
app.include_router(system_settings_router)
app.include_router(updates_router)
app.include_router(severity_patterns_global_router)
app.include_router(severity_patterns_source_router)
app.include_router(monitoring_router)
app.include_router(metrics_router)


@app.exception_handler(LocalPathNotAllowedError)
async def local_path_not_allowed(request: Request, exc: LocalPathNotAllowedError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


install_error_handlers(app)
# Kept importable for tests that build a bare app around one router.
path_outside_base = _path_outside_base


@app.get("/healthz", dependencies=[Depends(require_probe_access)])
async def healthz():
    """Liveness: the process is up and answers HTTP. Touches nothing else
    for a local or allowed caller, so a busy database never gets the
    container restarted. Who may call it: app/monitoring_access.py (#366)."""
    return {"status": "ok"}


@app.get("/readyz", dependencies=[Depends(require_probe_access)])
def readyz():
    """Readiness (#330): a database write works, DATA_DIR is writable and
    the scheduler runs. 503, naming what failed, when any doesn't. A sync
    endpoint, so the checks run in the thread pool. Who may call it:
    app/monitoring_access.py (#366)."""
    failures = readiness_failures(engine, get_settings().data_dir, scheduler)
    if failures:
        return JSONResponse(status_code=503, content={"status": "not_ready", "failed": failures})
    return {"status": "ready"}


# Serves the built frontend SPA (see Dockerfile's node build stage) at "/".
# Mounted last so it never shadows an API route above, and works unmodified
# with the frontend's hash-based routing (svelte-spa-router): the browser
# always requests "/" regardless of which view is active, so a plain static
# mount is enough — no catch-all/rewrite needed for client-side routes.
# Absent entirely in local dev when nobody has run `npm run build`.
_frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
