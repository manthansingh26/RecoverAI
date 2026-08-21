"""RecoverAI backend — FastAPI application entry point."""

from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.simulation import router as simulation_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.workflow import router as workflow_router
from app.core.config import settings


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="RecoverAI",
        description="Event-driven payment recovery system",
        version="0.1.0",
    )

    app.include_router(health_router)
    app.include_router(webhooks_router)
    app.include_router(simulation_router)
    app.include_router(workflow_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"message": "RecoverAI API", "env": settings.APP_ENV}

    return app


app = create_app()
