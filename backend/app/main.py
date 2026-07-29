import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.auth import router as auth_router
from app.db import init_db
from app.logging_config import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("startup.complete")
    yield


app = FastAPI(title="PerchTail", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
