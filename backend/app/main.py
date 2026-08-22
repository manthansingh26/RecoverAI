"""RecoverAI backend — FastAPI application entry point."""

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


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="RecoverAI",
        description="Event-driven payment recovery system",
        version="0.1.0",
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
