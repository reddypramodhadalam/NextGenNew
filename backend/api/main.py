"""
api/main.py

FastAPI application entry point.

Startup sequence:
1. Settings are validated (happens at import time in core/settings.py)
2. Database pool is initialised
3. Routers are registered
4. Central error middleware is registered

If any required environment variable is missing, the application fails
here with a clear error message — not mid-request, not silently.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.db.connection import init_pool
from core.logging_config import configure_logging
from core.settings import settings
from api.routes import run as run_routes
from api.routes import test_data as test_data_routes
from api.routes import dashboard as dashboard_routes
from api.routes import chat as chat_routes
from api.routes import chat_actions as chat_actions_routes
from api.routes import repository as repository_routes
from api.routes import healing as healing_routes
from api.routes import environments as environments_routes
from api.routes import suites as suites_routes
from api.routes import schedules as schedules_routes
from api.routes import imports as imports_routes
from api.routes import help as help_routes
from api.routes import support as support_routes
from api.routes import alm as alm_routes
from api.routes import app_credentials as app_credentials_routes
from api.routes import applications, test_suites, proposals, executions, reports

# Configure structured JSON logging to stdout before any other code runs.
# Level can be overridden with LOG_LEVEL env var (DEBUG, INFO, WARNING, ERROR).
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
configure_logging(level=_log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at startup and once at shutdown."""
    logger.info("NextGen2 backend starting")
    init_pool()
    logger.info("Database pool ready")

    # Background scheduler: wakes every 30s and runs whatever is due.
    from service import scheduler as scheduler_service
    scheduler_service.start()
    yield
    logger.info("NextGen2 backend shutting down")
    from service import scheduler as scheduler_service
    scheduler_service.stop()


app = FastAPI(
    title="NextGen2 API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allows the Next.js frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Central error handler ─────────────────────────────────────────────────────
# One place that maps typed exceptions to safe response shapes.
# The full exception and stack trace are logged on the backend.
# The frontend never receives a raw traceback.

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}},
    )


# ─── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health():
    """
    Returns the application's health status.
    Checks database connectivity. Used by monitoring, not business logic.
    """
    from core.db.connection import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        logger.error("Health check: database unreachable — %s", e)
        db_status = "error"

    return {"status": "ok" if db_status == "ok" else "degraded", "database": db_status}


# ─── Routers ───────────────────────────────────────────────────────────────────

app.include_router(applications.router, prefix="/v1", tags=["applications"])
app.include_router(test_suites.router, prefix="/v1", tags=["test-suites"])
app.include_router(proposals.router, prefix="/v1", tags=["proposals"])
app.include_router(executions.router, prefix="/v1", tags=["executions"])
app.include_router(reports.router, prefix="/v1", tags=["reports"])
app.include_router(run_routes.router, prefix="/v1", tags=["run"])
app.include_router(test_data_routes.router, prefix="/v1", tags=["test-data"])
app.include_router(dashboard_routes.router, prefix="/v1", tags=["dashboard"])
app.include_router(chat_routes.router, prefix="/v1", tags=["chat"])
app.include_router(healing_routes.router, prefix="/v1", tags=["healing"])
app.include_router(environments_routes.router, prefix="/v1", tags=["environments"])
app.include_router(suites_routes.router, prefix="/v1", tags=["suites"])
app.include_router(schedules_routes.router, prefix="/v1", tags=["schedules"])
app.include_router(imports_routes.router, prefix="/v1", tags=["imports"])
app.include_router(chat_actions_routes.router, prefix="/v1", tags=["chat-actions"])
app.include_router(repository_routes.router, prefix="/v1", tags=["repository"])
app.include_router(help_routes.router, prefix="/v1", tags=["help"])
app.include_router(support_routes.router, prefix="/v1", tags=["support"])
app.include_router(alm_routes.router, prefix="/v1", tags=["alm"])
app.include_router(app_credentials_routes.router, prefix="/v1", tags=["app-credentials"])