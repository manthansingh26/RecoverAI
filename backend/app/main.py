"""RecoverAI backend — FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.health import router as health_router
from app.api.routes.payments import router as payments_router
from app.api.routes.recovery_cases import router as recovery_cases_router
from app.api.routes.simulation import router as simulation_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.workflow import router as workflow_router
from app.core.config import settings
from app.services.recovery_scheduler import RecoveryScheduler


def _get_cors_origins() -> list[str]:
    """Return allowed CORS origins based on environment."""
    if settings.APP_ENV in ("development", "test"):
        return [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    # Production: restrict to known frontend origins
    return []


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan — manages background tasks across the application lifetime.

    Starts the recovery scheduler on application startup (when enabled) and
    ensures it is gracefully stopped before the process exits.

    The scheduler is an explicit opt-in (SCHEDULER_ENABLED=False by default)
    to prevent unintentional execution if EXECUTION_MODE is ever changed to
    RAZORPAY in a future deployment.

    Shutdown sequence:
    1. Signal shutdown_event so the loop won't start a new cycle.
    2. Cancel the asyncio task (wakes up any sleeping wait_for).
    3. Await the task so the event loop is clean before lifespan exits.
    The currently-running cycle is allowed to finish because each cycle
    commits per-case and execute_due_cases() is cooperative.
    """
    scheduler: RecoveryScheduler | None = None

    if settings.SCHEDULER_ENABLED:
        scheduler = RecoveryScheduler(
            interval_seconds=settings.SCHEDULER_INTERVAL_SECONDS
        )
        await scheduler.start()

    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="RecoverAI",
        description="Event-driven payment recovery system",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — only enabled when allowed origins are configured
    allowed_origins = _get_cors_origins()
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    app.include_router(health_router)
    app.include_router(webhooks_router)
    app.include_router(simulation_router)
    app.include_router(workflow_router)
    app.include_router(recovery_cases_router)
    app.include_router(dashboard_router)
    app.include_router(payments_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"message": "RecoverAI API", "env": settings.APP_ENV}

    return app


app = create_app()
