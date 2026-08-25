"""Health check endpoints.

/health        — liveness: process is alive (static, unchanged behavior).
/health/ready  — readiness: process + required dependencies (DB, and the
                 scheduler when it is enabled) are available.

The two are intentionally separate: an operator can distinguish "the process
is up but the database is unreachable" from "everything is ready".
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.recovery_scheduler import get_scheduler_status

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Return service health status (liveness)."""
    return {"status": "ok", "service": "recoverai-backend"}


@router.get("/health/ready")
async def readiness_check(
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return readiness: process + required dependencies available.

    1. Verifies database connectivity with ``SELECT 1``.
    2. When the scheduler is enabled, checks its running status.
    3. Returns 200 only when every required dependency is ready; otherwise 503.

    Readiness is infrastructure-facing and intentionally requires NO operator
    authentication so orchestrators/load-balancers can probe it.
    """
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — any DB failure means "not ready"
        db_ok = False

    scheduler_status = get_scheduler_status()
    if settings.SCHEDULER_ENABLED:
        scheduler_ready = scheduler_status.running
        scheduler_state = "running" if scheduler_ready else "not_running"
    else:
        scheduler_ready = True  # disabled scheduler is not a dependency
        scheduler_state = "disabled"

    ready = db_ok and scheduler_ready
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "ready": ready,
            "db": "ok" if db_ok else "unavailable",
            "scheduler": scheduler_state,
        },
    )