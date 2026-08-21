"""Dashboard API — summary metrics for the recovery system."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.recovery_case import DashboardSummary
from app.services.recovery_review import get_dashboard_summary

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/dashboard/summary")
async def dashboard_summary(
    db: Session = Depends(get_db),
) -> DashboardSummary:
    """Get dashboard summary metrics.

    Returns counts of cases by status, execution metrics,
    and human approval state across the entire system.
    """
    metrics = get_dashboard_summary(db)
    return DashboardSummary(**metrics)
