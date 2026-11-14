"""App-wide handlers for connector errors: a SourceError becomes its own
status with a message that's safe for anyone who can view the source, and
the underlying detail goes to the log with the request id (#170). Also the
one place every refused permission is audited (access_denied below)."""

import inspect

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import request_context
from app.audit import record_audit_event
from app.auth.rbac import PermissionDenied
from app.collectors.base import PathOutsideBaseError, SourceError
from app.db import get_session
from app.logging_config import get_logger
from app.scratch import ScratchFullError

logger = get_logger(__name__)

# Path parameters that name what a refused request was about, most specific
# first: /sources/{source_id}/rules/{rule_id} is about the rule.
_PATH_TARGETS = (
    ("rule_id", "rule"),
    ("pattern_id", "severity_pattern"),
    ("grant_id", "role_grant"),
    ("mapping_id", "sso_group_mapping"),
    ("provider_id", "sso_provider"),
    ("alert_id", "alert"),
    ("source_id", "source"),
    ("folder_id", "folder"),
    ("customer_id", "customer"),
    ("role_id", "role"),
    ("user_id", "user"),
)


def _target(path_params: dict) -> tuple[str | None, int | None]:
    for param, target_type in _PATH_TARGETS:
        raw = path_params.get(param)
        if raw is None:
            continue
        try:
            return target_type, int(raw)
        except (TypeError, ValueError):
            return target_type, None
    return None, None


def _reason(exc: StarletteHTTPException) -> str:
    if isinstance(exc, PermissionDenied):
        return exc.reason
    detail = exc.detail
    if isinstance(detail, dict):
        detail = detail.get("error_code") or detail.get("message")
    return str(detail)[:200] if detail else "forbidden"


def _request_session(request: Request):
    """A database session the way the request's own endpoints get one, so a
    test that points get_session at its database sees the entry. Returns
    (session, generator to close or None)."""
    provider = request.app.dependency_overrides.get(get_session, get_session)
    value = provider()
    if inspect.isgenerator(value):
        return next(value), value
    return value, None


def _record_denial(request: Request, exc: StarletteHTTPException, user_id: int) -> None:
    route = request.scope.get("route")
    attempted = f"{request.method} {getattr(route, 'path', request.url.path)}"
    target_type, target_id = _target(request.path_params)
    reason = _reason(exc)
    session, generator = _request_session(request)
    try:
        # Whatever the refused request left pending is never committed with
        # the entry.
        session.rollback()
        record_audit_event(
            session,
            user_id=user_id,
            action="access.denied",
            target_type=target_type,
            target_id=target_id,
            metadata={"attempted": attempted, "reason": reason},
        )
        session.commit()
    finally:
        if generator is not None:
            generator.close()
    logger.warning(
        "access.denied",
        user_id=user_id,
        attempted=attempted,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
    )


async def access_denied(request: Request, exc: StarletteHTTPException):
    """Every 403 a signed-in user gets is audited as `access.denied`, with
    what was attempted, on what, and why (the why only here: the answer the
    user gets is unchanged). Refusals an endpoint already audited itself
    (the file.*_denied events) aren't written twice. A 403 that isn't a
    permission refusal (the monitoring allowlist, where no user is known; a
    forced password change) isn't recorded. If recording fails, the
    refusal still goes out."""
    if exc.status_code == 403:
        context = request_context.current()
        if context is not None and context.user_id is not None and not context.denial_handled:
            try:
                await run_in_threadpool(_record_denial, request, exc, context.user_id)
            except Exception:  # noqa: BLE001 - never turn a 403 into a 500
                logger.exception("access.denied.audit_failed")
    return await http_exception_handler(request, exc)


async def source_error_handler(request: Request, exc: SourceError) -> JSONResponse:
    logger.warning(
        "source.error",
        error_type=type(exc).__name__,
        status_code=exc.status_code,
        detail=exc.detail,
        path=request.url.path,
    )
    content = {"detail": exc.public_message}
    if exc.error_code:
        content["error_code"] = exc.error_code
    return JSONResponse(status_code=exc.status_code, content=content)


async def path_outside_base(request: Request, exc: PathOutsideBaseError) -> JSONResponse:
    # Same answer as a rule-hidden path: nothing about the target leaks.
    return JSONResponse(status_code=404, content={"detail": "not found"})


async def scratch_full(request: Request, exc: ScratchFullError) -> JSONResponse:
    # 507 Insufficient Storage (#345): refused before anything was fetched.
    return JSONResponse(
        status_code=507,
        content={"detail": {"message": exc.message, "error_code": exc.reason}},
    )


_SQLITE_INTEGER_OVERFLOW = "too large to convert to SQLite INTEGER"


async def integer_out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
    # SQLite stores signed 64-bit integers; a larger one in the path or body
    # is invalid input, not a server error (#376). Any other OverflowError
    # is a bug and stays a 500.
    if _SQLITE_INTEGER_OVERFLOW not in str(exc):
        raise exc
    route = request.scope.get("route")
    logger.warning(
        "api.integer_out_of_range",
        method=request.method,
        # The route's template, not the URL: no values from the request.
        path=getattr(route, "path", None),
    )
    return JSONResponse(
        status_code=422, content={"detail": "A number in the request is out of range"}
    )


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, access_denied)
    app.add_exception_handler(SourceError, source_error_handler)
    app.add_exception_handler(PathOutsideBaseError, path_outside_base)
    app.add_exception_handler(ScratchFullError, scratch_full)
    app.add_exception_handler(OverflowError, integer_out_of_range)
