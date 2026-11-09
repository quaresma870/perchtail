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


def install(app: FastAPI) -> None:
    app.add_exception_handler(SourceError, source_error_handler)
    app.add_exception_handler(PathOutsideBaseError, path_outside_base)
    app.add_exception_handler(ScratchFullError, scratch_full)
