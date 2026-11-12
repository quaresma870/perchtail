"""App-wide handlers for connector errors: a SourceError becomes its own
status with a message that's safe for anyone who can view the source, and
the underlying detail goes to the log with the request id (#170)."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.collectors.base import PathOutsideBaseError, SourceError
from app.logging_config import get_logger
from app.scratch import ScratchFullError

logger = get_logger(__name__)


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
    app.add_exception_handler(SourceError, source_error_handler)
    app.add_exception_handler(PathOutsideBaseError, path_outside_base)
    app.add_exception_handler(ScratchFullError, scratch_full)
    app.add_exception_handler(OverflowError, integer_out_of_range)
