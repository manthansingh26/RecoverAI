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

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.enums import RecoveryStatus
from app.models.recovery_case import RecoveryCase
from app.schemas.recovery_case import (
    ExecutionLogSummary,
    ExecutionLogsResponse,
    ExecutionResponse,
    PaginationMeta,
    PaymentEventSummary,
    RecoveryCaseDetail,
    RecoveryCaseListItem,
    RecoveryCaseListResponse,
    RecoveryCheckoutResponse,
    ReviewActionResponse,
)
from app.services.payment_service import create_razorpay_order_internal
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
        resolved_strategy=result.resolved_strategy,
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


# ---------------------------------------------------------------------------
# POST /api/recovery-cases/{recovery_case_id}/recovery-checkout (Milestone 12)
# ---------------------------------------------------------------------------

def _lock_recovery_case(db: Session, case_uuid: uuid.UUID) -> RecoveryCase | None:
    """Fetch a RecoveryCase by id, taking a blocking row-level PostgreSQL lock.

    Uses `with_for_update()` (blocking) rather than `skip_locked=True` so a
    concurrent checkout request WAITS for the lock holder to finish instead of
    skipping the row (which would surface a false 404). This serializes
    concurrent checkouts against the same case.
    """
    return db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == case_uuid)
        .with_for_update()
    ).scalar_one_or_none()


@router.post(
    "/api/recovery-cases/{recovery_case_id}/recovery-checkout",
    response_model=RecoveryCheckoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Create or reuse a Razorpay Test recovery order for this case",
)
def create_or_reuse_recovery_checkout(
    recovery_case_id: str,
    db: Session = Depends(get_db),
) -> RecoveryCheckoutResponse:
    """Create or reuse a Razorpay Test Mode recovery order for customer checkout.

    Required behavior:
    1. Validates UUID and case existence.
    2. Rejects cases already in RESOLVED_SUCCESS.
    3. Rejects cases in RESOLVED_FAILED.
    4. If case already has an active recovery_order in decision_audit_trail,
       reuses and returns the existing order details (prevents duplicate orders).
    5. If no active order exists, creates a fresh Razorpay Test order with
       notes.recovery_case_id and notes.original_order_id.
    6. Persists recovery-order metadata inside decision_audit_trail["recovery_order"].
    7. Never returns RAZORPAY_KEY_SECRET.

    Concurrency design (double-checked creation):

      Phase A — row lock, read-only.
        Take a blocking `SELECT ... FOR UPDATE` on the RecoveryCase and check
        for an existing recovery_order. This serializes concurrent requests so
        only one can observe "no order exists" per lock hold.

      Phase B — release the lock, then create the order.
        The Razorpay order.create network call is made OUTSIDE the database
        transaction so a slow upstream does not pin a DB row lock.

      Phase C — re-lock and double-check.
        Re-acquire the row lock and re-read decision_audit_trail. If a
        concurrent request created an order while we were on the network, reuse
        it; otherwise persist our own. Exactly one recovery_order is ever
        persisted for a case.

    This guarantees at most one ACTIVE recovery_order in the audit trail per
    case. A concurrent loser may still have created an orphan Razorpay Test
    order (Test Mode only) but never persists or returns it.
    """
    case_uuid = _validate_uuid(recovery_case_id)

    # --- Phase A: lock the row and check for an existing recovery order ---
    rc = _lock_recovery_case(db, case_uuid)
    if rc is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    if rc.status == RecoveryStatus.RESOLVED_SUCCESS.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Recovery case is already successfully resolved.",
        )

    if rc.status == RecoveryStatus.RESOLVED_FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Recovery case is marked as failed. Cannot open new recovery checkout.",
        )

    pe = rc.payment_event
    if pe is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Recovery case has no associated payment event data",
        )

    # Check if an active recovery_order already exists in decision_audit_trail
    trail = dict(rc.decision_audit_trail or {})
    existing_rec_order = trail.get("recovery_order")

    if (
        isinstance(existing_rec_order, dict)
        and existing_rec_order.get("order_id")
        and settings.RAZORPAY_KEY_ID
    ):
        logger.info(
            "Reusing existing active recovery order %s for case %s",
            existing_rec_order["order_id"],
            rc.id,
        )
        # Commit to release the row lock before returning.
        db.commit()
        return RecoveryCheckoutResponse(
            key_id=settings.RAZORPAY_KEY_ID,
            order_id=existing_rec_order["order_id"],
            amount=existing_rec_order.get("amount_paise", pe.amount_paise),
            currency=existing_rec_order.get("currency", pe.currency),
            receipt=existing_rec_order.get("receipt", f"rcpt_rec_{str(rc.id)[:12]}"),
            recovery_case_id=str(rc.id),
            is_reused=True,
        )

    # Capture everything needed to build the order before releasing the lock,
    # because committing expires the ORM instance.
    case_id_str = str(rc.id)
    amount_paise = pe.amount_paise
    currency = pe.currency
    original_order_id = pe.external_order_id or ""
    retry_count = rc.retry_count

    # --- Phase B: release the lock, then make the network call ---
    db.commit()

    receipt = f"rcpt_rec_{case_id_str[:12]}"
    notes = {
        "recovery_case_id": case_id_str,
        "original_order_id": original_order_id,
        "recovery_attempt": str(retry_count),
    }

    result = create_razorpay_order_internal(
        amount_paise=amount_paise,
        currency=currency,
        receipt=receipt,
        notes=notes,
    )

    # --- Phase C: re-lock and double-check for a concurrent creation ---
    rc = _lock_recovery_case(db, case_uuid)
    if rc is None:
        # Case deleted while the order was being created. Surface 404; the
        # freshly-created Test order is left orphaned but never persisted.
        logger.warning(
            "Recovery case %s disappeared during order creation (order %s)",
            case_id_str,
            result.order_id,
        )
        raise HTTPException(status_code=404, detail="Recovery case not found")

    # Re-verify terminal safety states against the freshly-locked row.
    if rc.status in (
        RecoveryStatus.RESOLVED_SUCCESS.value,
        RecoveryStatus.RESOLVED_FAILED.value,
    ):
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Recovery case is no longer open for checkout.",
        )

    trail = dict(rc.decision_audit_trail or {})
    concurrent_order = trail.get("recovery_order")

    if (
        isinstance(concurrent_order, dict)
        and concurrent_order.get("order_id")
        and concurrent_order["order_id"] != result.order_id
        and settings.RAZORPAY_KEY_ID
    ):
        # Another request won the race while we were on the network. Reuse it.
        logger.info(
            "Reusing concurrently-created recovery order %s for case %s "
            "(discarding orphan order %s)",
            concurrent_order["order_id"],
            case_id_str,
            result.order_id,
        )
        db.commit()
        return RecoveryCheckoutResponse(
            key_id=settings.RAZORPAY_KEY_ID,
            order_id=concurrent_order["order_id"],
            amount=concurrent_order.get("amount_paise", amount_paise),
            currency=concurrent_order.get("currency", currency),
            receipt=concurrent_order.get("receipt", receipt),
            recovery_case_id=case_id_str,
            is_reused=True,
        )

    # We own this case's order — persist the metadata.
    trail["recovery_order"] = {
        "order_id": result.order_id,
        "amount_paise": result.amount_paise,
        "currency": result.currency,
        "receipt": result.receipt,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "created",
    }
    rc.decision_audit_trail = trail
    db.commit()

    logger.info(
        "Created new recovery order %s for case %s (amount=%d paise)",
        result.order_id,
        case_id_str,
        result.amount_paise,
    )

    return RecoveryCheckoutResponse(
        key_id=result.key_id,
        order_id=result.order_id,
        amount=result.amount_paise,
        currency=result.currency,
        receipt=result.receipt,
        recovery_case_id=case_id_str,
        is_reused=False,
    )
