"""RecoverAI backend — FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes.auth import router as auth_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.health import router as health_router
from app.api.routes.ops import router as ops_router
from app.api.routes.payments import router as payments_router
from app.api.routes.recovery_cases import router as recovery_cases_router
from app.api.routes.simulation import router as simulation_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.workflow import router as workflow_router
from app.core.config import settings
from app.core.logging import CorrelationIdMiddleware, configure_logging
from app.services.recovery_scheduler import (
    RecoveryScheduler,
    get_scheduler_status,
)


_DEV_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def _assert_production_safety() -> None:
    """Refuse to start with an unsafe production configuration.

    Called only when APP_ENV == "production". Raises RuntimeError (which
    aborts startup) for combinations that would silently weaken security.
    """
    problems: list[str] = []

    if not settings.RAZORPAY_WEBHOOK_SECRET:
        problems.append("RAZORPAY_WEBHOOK_SECRET must be set in production")
    if not settings.trusted_hosts_list:
        problems.append(
            "TRUSTED_HOSTS must be set in production (TrustedHostMiddleware "
            "is disabled when empty)"
        )
    if settings.DATABASE_URL.startswith(
        "postgresql+psycopg://recoverai:recoverai@localhost"
    ):
        problems.append(
            "DATABASE_URL must not be the default local development database "
            "in production"
        )
    if settings.EXECUTION_MODE.upper() not in ("SIMULATION", "RAZORPAY"):
        problems.append(
            f"Invalid EXECUTION_MODE '{settings.EXECUTION_MODE}'"
        )
    if settings.SCHEDULER_ENABLED and settings.EXECUTION_MODE.upper() == "RAZORPAY":
        problems.append(
            "SCHEDULER_ENABLED with EXECUTION_MODE=RAZORPAY could trigger "
            "real financial actions without explicit confirmation — refusing "
            "to start. Set EXECUTION_MODE=SIMULATION or SCHEDULER_ENABLED=false."
        )

    if problems:
        raise RuntimeError(
            "Refusing to start RecoverAI in production with an unsafe "
            "configuration:\n  - " + "\n  - ".join(problems)
        )


def _get_cors_origins() -> list[str]:
    """Return allowed CORS origins based on environment.

    Development/test: allow the local frontend origins (unchanged behavior).
    Production: allow ONLY the origins explicitly configured in CORS_ORIGINS.
    An empty CORS_ORIGINS in production disables cross-origin access entirely
    (the middleware is only registered when the list is non-empty).

    "*" is never allowed: the middleware uses allow_credentials=True, and a
    wildcard origin combined with credentials is a security anti-pattern.
    """
    if settings.APP_ENV in ("development", "test"):
        return list(_DEV_CORS_ORIGINS)
    # Production: allowed origins come ONLY from the configured CORS_ORIGINS.
    return settings.cors_origins_list


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

    # Expose scheduler heartbeat/status to routes (e.g. /health/ready).
    app.state.scheduler_status = get_scheduler_status()

    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Structured JSON logging + LOG_LEVEL wiring (Milestone 15B).
    configure_logging()

    # Refuse to start when the production configuration is unsafe.
    if settings.APP_ENV == "production":
        _assert_production_safety()

    app = FastAPI(
        title="RecoverAI",
        description="Event-driven payment recovery system",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Correlation ID middleware — pure-ASGI, never reads the request body,
    # so the webhook raw-body HMAC pipeline is completely unaffected.
    app.add_middleware(CorrelationIdMiddleware)

    # TrustedHostMiddleware — only applies a Host allowlist when configured.
    # The production startup assertion requires TRUSTED_HOSTS in production.
    if settings.trusted_hosts_list:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=settings.trusted_hosts_list,
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
    app.include_router(auth_router)
    app.include_router(simulation_router)
    app.include_router(workflow_router)
    app.include_router(recovery_cases_router)
    app.include_router(dashboard_router)
    app.include_router(payments_router)
    app.include_router(ops_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        # Public probe route. Intentionally does NOT leak APP_ENV or any
        # other environment detail (Milestone 14A).
        return {"message": "RecoverAI API"}

    return app


app = create_app()
