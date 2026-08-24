"""Dashboard API — summary metrics and analytics for the recovery system."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core.roles import Permission
from app.db.session import get_db
from app.models.operator import Operator
from app.schemas.recovery_case import (
    ActivityFeed,
    DashboardAnalytics,
    DashboardSummary,
)
from app.services.recovery_review import (
    get_dashboard_activity,
    get_dashboard_analytics,
    get_dashboard_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/dashboard/summary")
async def dashboard_summary(
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_permission(Permission.VIEW_DASHBOARD)),
) -> DashboardSummary:
    """Get dashboard summary metrics.

    Returns counts of cases by status, execution metrics,
    and human approval state across the entire system.
    Requires VIEW_DASHBOARD permission.
    """
    metrics = get_dashboard_summary(db)
    return DashboardSummary(**metrics)


@router.get("/api/dashboard/analytics")
async def dashboard_analytics(
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_permission(Permission.VIEW_ANALYTICS)),
) -> DashboardAnalytics:
    """Get comprehensive analytics for the Recovery Intelligence dashboard.

    Returns status/strategy distributions, performance metrics,
    financial impact, human review state, and daily activity.
    All data is derived from actual database records.
    Requires VIEW_ANALYTICS permission.
    """
    analytics = get_dashboard_analytics(db)
    return DashboardAnalytics(**analytics)


@router.get("/api/dashboard/activity")
async def dashboard_activity(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_permission(Permission.VIEW_ACTIVITY)),
) -> ActivityFeed:
    """Get live recovery activity feed derived from real database records.

    Returns deterministic chronologically sorted activity items with stable IDs.
    Requires VIEW_ACTIVITY permission.
    """
    activity_data = get_dashboard_activity(db, limit=limit)
    return ActivityFeed(**activity_data)
