import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.agent_registry import get_agent_registry
from app.api.agent_ws import router as agent_ws_router
from app.api.alerts import router as alerts_router
from app.api.archive import router as archive_router
from app.api.auth import router as auth_router
from app.api.customers import router as customers_router
from app.api.folders import router as folders_router
from app.api.monitoring import router as monitoring_router
from app.api.roles import router as roles_router
from app.api.rules import router as rules_router
from app.api.search import router as search_router
from app.api.severity_patterns import global_router as severity_patterns_global_router
from app.api.severity_patterns import source_router as severity_patterns_source_router
from app.api.sources import router as sources_router
from app.api.sso import router as sso_router
from app.api.system_settings import router as system_settings_router
from app.api.users import router as users_router
from app.bootstrap import (
    seed_initial_super_admin,
    seed_no_access_role,
    seed_severity_patterns,
    seed_system_log_source,
)
from app.config import get_settings
from app.db import engine, init_db
from app.health import mark_started
from app.logging_config import configure_logging, get_logger
from app.scheduler import scheduler
from app.scratch import get_scratch_store
from app.search_index import run_indexing_sweep
from app.version import APP_VERSION

configure_logging()
logger = get_logger(__name__)

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
        return response


_ORIGIN_CHECKED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_same_origin(candidate: str, expected_origin: str) -> bool:
    """candidate is a raw Origin header value, or a full Referer URL -- both
    get reduced to scheme://host[:port] before comparing, since Referer
    carries a path/query Origin never does."""
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}" == expected_origin


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
    If neither header is present at all, the request is let through to
    rely on SameSite alone -- rejecting on a merely absent header would
    also break legitimate non-browser API use (curl, scripts) that a
    security review shouldn't introduce as a side effect."""

    async def dispatch(self, request: Request, call_next):
        if request.method in _ORIGIN_CHECKED_METHODS:
            candidate = request.headers.get("origin") or request.headers.get("referer")
            if candidate is not None:
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if not _is_same_origin(candidate, expected):
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

    init_db()
    mark_started()
    get_agent_registry().bind_loop(asyncio.get_running_loop())
    with Session(engine) as session:
        seed_system_log_source(session)
        seed_initial_super_admin(session)
        seed_no_access_role(session)
        seed_severity_patterns(session)

    store = get_scratch_store()
    scheduler.add_job(
        store.sweep_idle,
        "interval",
        seconds=settings.scratch_sweep_interval_seconds,
        kwargs={"idle_seconds": settings.scratch_idle_seconds},
    )
    scheduler.add_job(
        store.enforce_size_guard,
        "interval",
        seconds=settings.scratch_sweep_interval_seconds,
    )
    scheduler.add_job(
        run_indexing_sweep,
        "interval",
        seconds=settings.search_index_interval_seconds,
    )
    scheduler.start()
    logger.info("startup.complete")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="PerchTail", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
app.include_router(alerts_router)
app.include_router(archive_router)
app.include_router(customers_router)
app.include_router(folders_router)
app.include_router(sources_router)
app.include_router(rules_router)
app.include_router(roles_router)
app.include_router(users_router)
app.include_router(sso_router)
app.include_router(agent_ws_router)
app.include_router(search_router)
app.include_router(system_settings_router)
app.include_router(severity_patterns_global_router)
app.include_router(severity_patterns_source_router)
app.include_router(monitoring_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# Serves the built frontend SPA (see Dockerfile's node build stage) at "/".
# Mounted last so it never shadows an API route above, and works unmodified
# with the frontend's hash-based routing (svelte-spa-router): the browser
# always requests "/" regardless of which view is active, so a plain static
# mount is enough — no catch-all/rewrite needed for client-side routes.
# Absent entirely in local dev when nobody has run `npm run build`.
_frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
