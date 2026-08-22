"""Recovery Review Service — human approval/rejection and dashboard operations.

Provides:
- List recovery cases with filtering and pagination
- Get case detail with payment event and execution history
- Approve a case requiring human review
- Reject a case requiring human review
- Dashboard summary metrics
- Manual safe execution trigger

All operations are idempotent and concurrency-safe where applicable.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, cast, func, select, Date
from sqlalchemy.orm import Session

from app.models.enums import (
    ExecutionStatus,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.execution_log import ExecutionLog
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ReviewActionResult:
    """Result of an approval or rejection action."""

    recovery_case_id: str
    previous_status: str
    new_status: str
    previous_approved_by_human: bool | None
    new_approved_by_human: bool | None
    action: str
    message: str


# ---------------------------------------------------------------------------
# List recovery cases
# ---------------------------------------------------------------------------

def list_recovery_cases(
    db: Session,
    *,
    status: str | None = None,
    strategy: str | None = None,
    requires_human_approval: bool | None = None,
    approved_by_human: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[RecoveryCase], int]:
    """List recovery cases with optional filters and pagination.

    Returns:
        (cases, total_count)
    """
    stmt = select(RecoveryCase)

    if status is not None:
        stmt = stmt.where(RecoveryCase.status == status)
    if strategy is not None:
        stmt = stmt.where(RecoveryCase.recommended_strategy == strategy)
    if requires_human_approval is not None:
        stmt = stmt.where(RecoveryCase.requires_human_approval == requires_human_approval)
    if approved_by_human is not None:
        stmt = stmt.where(RecoveryCase.approved_by_human == approved_by_human)

    # Get total count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_stmt).scalar() or 0

    # Apply ordering and pagination
    stmt = stmt.order_by(RecoveryCase.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    cases = list(db.execute(stmt).scalars().all())
    return cases, total


# ---------------------------------------------------------------------------
# Get case detail
# ---------------------------------------------------------------------------

def get_case_detail(
    db: Session,
    recovery_case_id: str,
) -> RecoveryCase | None:
    """Get a RecoveryCase by ID with relationships loaded.

    Returns:
        RecoveryCase or None if not found.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return None

    rc = db.get(RecoveryCase, rc_uuid)
    return rc


def get_execution_logs(
    db: Session,
    recovery_case_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ExecutionLog], int]:
    """Get execution logs for a case with pagination.

    Returns:
        (logs, total_count)
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return [], 0

    stmt = select(ExecutionLog).where(
        ExecutionLog.recovery_case_id == rc_uuid
    )

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_stmt).scalar() or 0

    # Order newest first, paginate
    stmt = stmt.order_by(ExecutionLog.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    logs = list(db.execute(stmt).scalars().all())
    return logs, total


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def approve_case(
    db: Session,
    recovery_case_id: str,
) -> ReviewActionResult | None:
    """Approve a recovery case requiring human review.

    Idempotent: repeated approval does not corrupt state.
    Concurrency-safe: uses row locking.

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase.

    Returns:
        ReviewActionResult or None if case not found.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return None

    # Lock the row
    rc = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == rc_uuid)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if rc is None:
        return None

    previous_status = rc.status
    previous_approved = rc.approved_by_human

    # If already approved, return idempotent result
    if rc.approved_by_human is True:
        return ReviewActionResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            previous_approved_by_human=previous_approved,
            new_approved_by_human=rc.approved_by_human,
            action="approved",
            message="Case was already approved",
        )

    # If rejected, cannot re-approve
    if rc.approved_by_human is False:
        return ReviewActionResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            previous_approved_by_human=previous_approved,
            new_approved_by_human=rc.approved_by_human,
            action="approval_failed",
            message="Case was previously rejected and cannot be re-approved",
        )

    # Perform approval
    rc.approved_by_human = True

    # If case was in REQUIRES_HUMAN and has a valid strategy, transition
    # to PENDING_EXECUTION so it can be executed
    if rc.status == RecoveryStatus.REQUIRES_HUMAN.value:
        # Only transition if there's a valid executable strategy
        if rc.recommended_strategy in (
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.CREATE_PAYMENT_LINK.value,
        ):
            rc.status = RecoveryStatus.PENDING_EXECUTION.value
            # Set next_run_at to now so it becomes immediately due
            rc.next_run_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(rc)

    logger.info(
        "Case %s approved: %s -> %s",
        recovery_case_id, previous_status, rc.status,
    )

    return ReviewActionResult(
        recovery_case_id=recovery_case_id,
        previous_status=previous_status,
        new_status=rc.status,
        previous_approved_by_human=previous_approved,
        new_approved_by_human=rc.approved_by_human,
        action="approved",
        message=f"Case approved: {previous_status} -> {rc.status}",
    )


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

def reject_case(
    db: Session,
    recovery_case_id: str,
) -> ReviewActionResult | None:
    """Reject a recovery case requiring human review.

    Idempotent: repeated rejection does not corrupt state.
    A rejected case can never be auto-executed.

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase.

    Returns:
        ReviewActionResult or None if case not found.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return None

    # Lock the row
    rc = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == rc_uuid)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if rc is None:
        return None

    previous_status = rc.status
    previous_approved = rc.approved_by_human

    # If already rejected, return idempotent result
    if rc.approved_by_human is False:
        return ReviewActionResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            previous_approved_by_human=previous_approved,
            new_approved_by_human=rc.approved_by_human,
            action="rejected",
            message="Case was already rejected",
        )

    # Perform rejection
    rc.approved_by_human = False

    # Transition to RESOLVED_FAILED — rejected cases stop recovery
    rc.status = RecoveryStatus.RESOLVED_FAILED.value
    rc.next_run_at = None
    rc.requires_human_approval = False

    db.commit()
    db.refresh(rc)

    logger.info(
        "Case %s rejected: %s -> %s",
        recovery_case_id, previous_status, rc.status,
    )

    return ReviewActionResult(
        recovery_case_id=recovery_case_id,
        previous_status=previous_status,
        new_status=rc.status,
        previous_approved_by_human=previous_approved,
        new_approved_by_human=rc.approved_by_human,
        action="rejected",
        message=f"Case rejected: {previous_status} -> {rc.status}",
    )


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------

def get_dashboard_summary(db: Session) -> dict[str, int]:
    """Compute dashboard summary metrics from actual database data.

    Returns:
        Dict with metric names and counts.
    """
    # Case counts by status
    status_counts = {}
    for status_val in RecoveryStatus:
        count = db.execute(
            select(func.count()).select_from(RecoveryCase).where(
                RecoveryCase.status == status_val.value
            )
        ).scalar() or 0
        status_counts[status_val.value] = count

    total_cases = sum(status_counts.values())

    # Human approval metrics
    awaiting_human = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.requires_human_approval == True,  # noqa: E712
            RecoveryCase.approved_by_human.is_(None),
        )
    ).scalar() or 0

    approved_cases = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.approved_by_human == True,  # noqa: E712
        )
    ).scalar() or 0

    # Execution metrics
    total_executions = db.execute(
        select(func.count()).select_from(ExecutionLog)
    ).scalar() or 0

    execution_status_counts = {}
    for exec_status in ExecutionStatus:
        count = db.execute(
            select(func.count()).select_from(ExecutionLog).where(
                ExecutionLog.status == exec_status.value
            )
        ).scalar() or 0
        execution_status_counts[exec_status.value] = count

    return {
        "total_cases": total_cases,
        "received_cases": status_counts.get(RecoveryStatus.RECEIVED.value, 0),
        "pending_execution_cases": status_counts.get(RecoveryStatus.PENDING_EXECUTION.value, 0),
        "requires_human_cases": status_counts.get(RecoveryStatus.REQUIRES_HUMAN.value, 0),
        "resolved_success_cases": status_counts.get(RecoveryStatus.RESOLVED_SUCCESS.value, 0),
        "resolved_failed_cases": status_counts.get(RecoveryStatus.RESOLVED_FAILED.value, 0),
        "awaiting_human_review": awaiting_human,
        "approved_cases": approved_cases,
        "total_execution_attempts": total_executions,
        "successful_executions": execution_status_counts.get(ExecutionStatus.SUCCESS.value, 0),
        "failed_executions": execution_status_counts.get(ExecutionStatus.FAILED.value, 0),
        "blocked_executions": execution_status_counts.get(ExecutionStatus.BLOCKED.value, 0),
    }


# ---------------------------------------------------------------------------
# Dashboard analytics (Milestone 9A)
# ---------------------------------------------------------------------------

def get_dashboard_analytics(db: Session) -> dict[str, Any]:
    """Compute comprehensive analytics from actual database data.

    Uses efficient SQL aggregation queries — no N+1 problems.

    Returns:
        Dict matching the DashboardAnalytics schema shape.
    """
    # ----- 1. Status distribution (GROUP BY) -----
    status_rows = db.execute(
        select(
            RecoveryCase.status,
            func.count().label("cnt"),
        )
        .group_by(RecoveryCase.status)
    ).all()

    status_distribution = [
        {"status": row.status, "count": row.cnt}
        for row in status_rows
    ]

    # Build a quick lookup for performance metrics
    status_lookup: dict[str, int] = {row.status: row.cnt for row in status_rows}
    total_cases = sum(status_lookup.values())

    # ----- 2. Strategy distribution (GROUP BY) -----
    strategy_rows = db.execute(
        select(
            RecoveryCase.recommended_strategy,
            func.count().label("cnt"),
        )
        .where(RecoveryCase.recommended_strategy.is_not(None))
        .group_by(RecoveryCase.recommended_strategy)
    ).all()

    strategy_distribution = [
        {"strategy": row.recommended_strategy, "count": row.cnt}
        for row in strategy_rows
    ]

    # ----- 3. Recovery performance metrics -----
    successful_cases = status_lookup.get(RecoveryStatus.RESOLVED_SUCCESS.value, 0)
    failed_cases = status_lookup.get(RecoveryStatus.RESOLVED_FAILED.value, 0)
    human_review_cases = status_lookup.get(RecoveryStatus.REQUIRES_HUMAN.value, 0)

    # Pending = RECEIVED + DECISION_PENDING + PENDING_EXECUTION + EXECUTING
    pending_cases = sum(
        status_lookup.get(s.value, 0)
        for s in (
            RecoveryStatus.RECEIVED,
            RecoveryStatus.DECISION_PENDING,
            RecoveryStatus.PENDING_EXECUTION,
            RecoveryStatus.EXECUTING,
        )
    )

    resolved_total = successful_cases + failed_cases
    success_rate = (
        round(successful_cases / resolved_total * 100, 1)
        if resolved_total > 0
        else 0.0
    )

    performance = {
        "total_cases": total_cases,
        "successful_cases": successful_cases,
        "failed_cases": failed_cases,
        "pending_cases": pending_cases,
        "human_review_cases": human_review_cases,
        "success_rate": success_rate,
    }

    # ----- 4. Financial metrics (JOIN with PaymentEvent) -----
    # Single query: SUM amounts grouped by case status categories
    financial_rows = db.execute(
        select(
            func.coalesce(func.sum(PaymentEvent.amount_paise), 0).label("total"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RecoveryCase.status == RecoveryStatus.RESOLVED_SUCCESS.value,
                            PaymentEvent.amount_paise,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("recovered"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RecoveryCase.status.in_([
                                RecoveryStatus.RECEIVED.value,
                                RecoveryStatus.DECISION_PENDING.value,
                                RecoveryStatus.PENDING_EXECUTION.value,
                                RecoveryStatus.EXECUTING.value,
                            ]),
                            PaymentEvent.amount_paise,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pending"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RecoveryCase.status == RecoveryStatus.REQUIRES_HUMAN.value,
                            PaymentEvent.amount_paise,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("human_review"),
        )
        .select_from(RecoveryCase)
        .join(PaymentEvent, RecoveryCase.payment_event_id == PaymentEvent.id)
    ).one()

    financial = {
        "total_failed_amount_paise": int(financial_rows.total),
        "simulated_recovered_amount_paise": int(financial_rows.recovered),
        "pending_recovery_amount_paise": int(financial_rows.pending),
        "human_review_amount_paise": int(financial_rows.human_review),
    }

    # ----- 5. Human review metrics -----
    awaiting_review = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.requires_human_approval == True,  # noqa: E712
            RecoveryCase.approved_by_human.is_(None),
        )
    ).scalar() or 0

    approved = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.approved_by_human == True,  # noqa: E712
        )
    ).scalar() or 0

    rejected = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.approved_by_human == False,  # noqa: E712
        )
    ).scalar() or 0

    human_review = {
        "awaiting_review": awaiting_review,
        "approved": approved,
        "rejected": rejected,
    }

    # ----- 6. Daily activity (last 30 days) -----
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    daily_rows = db.execute(
        select(
            cast(RecoveryCase.created_at, Date).label("day"),
            func.count().label("cnt"),
        )
        .where(RecoveryCase.created_at >= thirty_days_ago)
        .group_by("day")
        .order_by("day")
    ).all()

    daily_activity = [
        {"date": row.day, "count": row.cnt}
        for row in daily_rows
    ]

    return {
        "status_distribution": status_distribution,
        "strategy_distribution": strategy_distribution,
        "performance": performance,
        "financial": financial,
        "human_review": human_review,
        "daily_activity": daily_activity,
    }

