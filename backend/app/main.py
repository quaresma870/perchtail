import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.agent_registry import get_agent_registry
from app.api.agent_ws import router as agent_ws_router
from app.api.archive import router as archive_router
from app.api.auth import router as auth_router
from app.api.customers import router as customers_router
from app.api.folders import router as folders_router
from app.api.roles import router as roles_router
from app.api.rules import router as rules_router
from app.api.search import router as search_router
from app.api.sources import router as sources_router
from app.api.sso import router as sso_router
from app.api.system_settings import router as system_settings_router
from app.api.users import router as users_router
from app.bootstrap import seed_initial_super_admin, seed_no_access_role, seed_system_log_source
from app.config import get_settings
from app.db import engine, init_db
from app.logging_config import configure_logging, get_logger
from app.scratch import get_scratch_store
from app.search_index import run_indexing_sweep

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
    get_agent_registry().bind_loop(asyncio.get_running_loop())
    with Session(engine) as session:
        seed_system_log_source(session)
        seed_initial_super_admin(session)
        seed_no_access_role(session)

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
    scheduler.add_job(
        run_indexing_sweep,
        "interval",
        seconds=settings.search_index_interval_seconds,
    )
    scheduler.start()
    logger.info("startup.complete")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="PerchTail", version="0.1.1", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(auth_router)
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
