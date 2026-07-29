import uuid
from contextlib import asynccontextmanager

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.archive import router as archive_router
from app.api.auth import router as auth_router
from app.api.customers import router as customers_router
from app.api.folders import router as folders_router
from app.api.roles import router as roles_router
from app.api.rules import router as rules_router
from app.api.sources import router as sources_router
from app.api.users import router as users_router
from app.config import get_settings
from app.db import init_db
from app.logging_config import configure_logging, get_logger
from app.scratch import get_scratch_store

configure_logging()
logger = get_logger(__name__)
scheduler = BackgroundScheduler()


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
    settings = get_settings()
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
    scheduler.start()
    logger.info("startup.complete")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="PerchTail", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
app.include_router(archive_router)
app.include_router(customers_router)
app.include_router(folders_router)
app.include_router(sources_router)
app.include_router(rules_router)
app.include_router(roles_router)
app.include_router(users_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
