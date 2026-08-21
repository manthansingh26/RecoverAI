"""Recovery Cases API — dashboard, review, and manual execution endpoints.

Provides:
- GET /api/recovery-cases — list with filtering and pagination
- GET /api/recovery-cases/{id} — case detail with payment event and logs
- GET /api/recovery-cases/{id}/execution-logs — paginated execution history
- POST /api/recovery-cases/{id}/approve — approve human-review case
- POST /api/recovery-cases/{id}/reject — reject human-review case
- POST /api/recovery-cases/{id}/execute — manual safe execution (dev only)
"""

import logging
import math
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.recovery_case import (
    ExecutionLogSummary,
    ExecutionLogsResponse,
    ExecutionResponse,
    PaginationMeta,
    PaymentEventSummary,
    RecoveryCaseDetail,
    RecoveryCaseListItem,
    RecoveryCaseListResponse,
    ReviewActionResponse,
)
from app.services.recovery_executor import execute_single_case
from app.services.recovery_review import (
    approve_case,
    get_case_detail,
    get_execution_logs,
    get_dashboard_summary,
    list_recovery_cases,
    reject_case,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_uuid(value: str) -> uuid.UUID:
    """Validate and parse a UUID string. Raises HTTPException 400 if invalid."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {value}")


def _clamp_page(value: int) -> int:
    """Ensure page is at least 1."""
    return max(1, value)


def _clamp_page_size(value: int) -> int:
    """Ensure page_size is between 1 and 100."""
    return max(1, min(100, value))


def _make_pagination(total: int, page: int, page_size: int) -> PaginationMeta:
    """Build pagination metadata."""
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    return PaginationMeta(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


def _case_to_list_item(rc) -> RecoveryCaseListItem:
    """Convert a RecoveryCase ORM object to a list item schema."""
    return RecoveryCaseListItem(
        recovery_case_id=str(rc.id),
        status=rc.status,
        failure_category=rc.failure_category,
        recommended_strategy=rc.recommended_strategy,
        retry_count=rc.retry_count,
        next_run_at=rc.next_run_at,
        requires_human_approval=rc.requires_human_approval,
        approved_by_human=rc.approved_by_human,
        created_at=rc.created_at,
        updated_at=rc.updated_at,
    )


def _payment_event_to_summary(pe) -> PaymentEventSummary:
    """Convert a PaymentEvent ORM object to a summary schema."""
    return PaymentEventSummary(
        payment_event_id=str(pe.id),
        event_type=pe.event_type,
        external_payment_id=pe.external_payment_id,
        external_order_id=pe.external_order_id,
        amount_paise=pe.amount_paise,
        currency=pe.currency,
        error_code=pe.error_code,
        error_reason=pe.error_reason,
        error_description=pe.error_description,
        created_at=pe.created_at,
    )


def _execution_log_to_summary(log) -> ExecutionLogSummary:
    """Convert an ExecutionLog ORM object to a summary schema."""
    return ExecutionLogSummary(
        execution_log_id=str(log.id),
        action=log.action,
        execution_mode=log.execution_mode,
        status=log.status,
        request_data=log.request_data or {},
        response_data=log.response_data or {},
        error_message=log.error_message,
        executed_at=log.executed_at,
        created_at=log.created_at,
    )


# ---------------------------------------------------------------------------
# GET /api/recovery-cases
# ---------------------------------------------------------------------------

@router.get("/api/recovery-cases")
async def list_cases(
    status: str | None = Query(None, description="Filter by status"),
    strategy: str | None = Query(None, description="Filter by strategy"),
    requires_human_approval: bool | None = Query(None, description="Filter by requires_human_approval"),
    approved_by_human: bool | None = Query(None, description="Filter by approved_by_human"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
) -> RecoveryCaseListResponse:
    """List recovery cases with optional filtering and pagination.

    Supports filtering by:
    - status: RecoveryStatus value
    - strategy: RecoveryStrategy value
    - requires_human_approval: boolean
    - approved_by_human: boolean or null

    Returns paginated results with metadata.
    """
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size)

    cases, total = list_recovery_cases(
        db,
        status=status,
        strategy=strategy,
        requires_human_approval=requires_human_approval,
        approved_by_human=approved_by_human,
        page=page,
        page_size=page_size,
    )

    items = [_case_to_list_item(rc) for rc in cases]

    return RecoveryCaseListResponse(
        items=items,
        pagination=_make_pagination(total, page, page_size),
    )


# ---------------------------------------------------------------------------
# GET /api/recovery-cases/{recovery_case_id}
# ---------------------------------------------------------------------------

@router.get("/api/recovery-cases/{recovery_case_id}")
async def get_case(
    recovery_case_id: str,
    db: Session = Depends(get_db),
) -> RecoveryCaseDetail:
    """Get detailed recovery case information.

    Includes:
    - Recovery case details
    - Related payment event summary
    - Recent execution logs (last 10)
    - Decision audit trail
    """
    _validate_uuid(recovery_case_id)

    rc = get_case_detail(db, recovery_case_id)
    if rc is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    # Payment event
    payment_event = None
    if rc.payment_event is not None:
        payment_event = _payment_event_to_summary(rc.payment_event)

    # Recent execution logs (last 10)
    logs, _ = get_execution_logs(db, recovery_case_id, page=1, page_size=10)
    recent_logs = [_execution_log_to_summary(log) for log in logs]

    return RecoveryCaseDetail(
        recovery_case_id=str(rc.id),
        status=rc.status,
        failure_category=rc.failure_category,
        recovery_probability=float(rc.recovery_probability) if rc.recovery_probability is not None else None,
        priority_score=float(rc.priority_score) if rc.priority_score is not None else None,
        recommended_strategy=rc.recommended_strategy,
        expected_value_paise=rc.expected_value_paise,
        retry_count=rc.retry_count,
        next_run_at=rc.next_run_at,
        requires_human_approval=rc.requires_human_approval,
        approved_by_human=rc.approved_by_human,
        created_at=rc.created_at,
        updated_at=rc.updated_at,
        payment_event=payment_event,
        recent_execution_logs=recent_logs,
        decision_audit_trail=rc.decision_audit_trail or {},
    )


# ---------------------------------------------------------------------------
# GET /api/recovery-cases/{recovery_case_id}/execution-logs
# ---------------------------------------------------------------------------

@router.get("/api/recovery-cases/{recovery_case_id}/execution-logs")
async def get_logs(
    recovery_case_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ExecutionLogsResponse:
    """Get paginated execution logs for a recovery case.

    Returns logs ordered newest first.
    """
    _validate_uuid(recovery_case_id)

    # Verify case exists
    rc = get_case_detail(db, recovery_case_id)
    if rc is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size)

    logs, total = get_execution_logs(
        db, recovery_case_id, page=page, page_size=page_size
    )

    items = [_execution_log_to_summary(log) for log in logs]

    return ExecutionLogsResponse(
        items=items,
        pagination=_make_pagination(total, page, page_size),
    )


# ---------------------------------------------------------------------------
# POST /api/recovery-cases/{recovery_case_id}/approve
# ---------------------------------------------------------------------------

@router.post("/api/recovery-cases/{recovery_case_id}/approve")
async def approve(
    recovery_case_id: str,
    db: Session = Depends(get_db),
) -> ReviewActionResponse:
    """Approve a recovery case requiring human review.

    - Idempotent: repeated approval returns success without corruption.
    - Concurrency-safe: uses row locking.
    - Does NOT execute any financial action.
    """
    _validate_uuid(recovery_case_id)

    result = approve_case(db, recovery_case_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    return ReviewActionResponse(
        recovery_case_id=result.recovery_case_id,
        previous_status=result.previous_status,
        new_status=result.new_status,
        previous_approved_by_human=result.previous_approved_by_human,
        new_approved_by_human=result.new_approved_by_human,
        action=result.action,
        message=result.message,
    )


# ---------------------------------------------------------------------------
# POST /api/recovery-cases/{recovery_case_id}/reject
# ---------------------------------------------------------------------------

@router.post("/api/recovery-cases/{recovery_case_id}/reject")
async def reject(
    recovery_case_id: str,
    db: Session = Depends(get_db),
) -> ReviewActionResponse:
    """Reject a recovery case requiring human review.

    - Idempotent: repeated rejection returns success without corruption.
    - A rejected case transitions to RESOLVED_FAILED.
    - A rejected case can never be auto-executed.
    """
    _validate_uuid(recovery_case_id)

    result = reject_case(db, recovery_case_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    return ReviewActionResponse(
        recovery_case_id=result.recovery_case_id,
        previous_status=result.previous_status,
        new_status=result.new_status,
        previous_approved_by_human=result.previous_approved_by_human,
        new_approved_by_human=result.new_approved_by_human,
        action=result.action,
        message=result.message,
    )


# ---------------------------------------------------------------------------
# POST /api/recovery-cases/{recovery_case_id}/execute
# ---------------------------------------------------------------------------

def _is_dev_or_test() -> bool:
    """Check if manual execution is enabled."""
    return settings.APP_ENV in ("development", "test")


@router.post("/api/recovery-cases/{recovery_case_id}/execute")
async def manual_execute(
    recovery_case_id: str,
    db: Session = Depends(get_db),
) -> ExecutionResponse:
    """Manually trigger execution of a recovery case.

    Only available in development/test environments.
    Uses existing execute_single_case() with all safety checks intact.
    Does NOT bypass eligibility, human approval, or idempotency.
    Does NOT execute real financial actions.
    """
    if not _is_dev_or_test():
        raise HTTPException(
            status_code=404,
            detail="Manual execution not available in this environment",
        )

    _validate_uuid(recovery_case_id)

    result = execute_single_case(db, recovery_case_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    return ExecutionResponse(
        recovery_case_id=result.recovery_case_id,
        strategy=result.strategy,
        execution_mode=result.execution_mode,
        status=result.status,
        previous_case_status=result.previous_case_status,
        new_case_status=result.new_case_status,
        message=result.message,
    )
