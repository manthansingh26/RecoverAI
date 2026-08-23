"""Recovery Resolver — validates and correlates Razorpay payment.captured webhooks to RecoveryCases.

Canonical Resolution Architecture:
1. payment.captured is the ONLY event that transitions a RecoveryCase to RESOLVED_SUCCESS.
2. Correlation hierarchy:
   - PRIMARY: notes.recovery_case_id (O(1) PK lookup).
   - SECONDARY: order_id match against original PaymentEvent.external_order_id (indexed).
   - AUDIT RECOVERY ORDER: order_id match against RecoveryCase.decision_audit_trail["recovery_order"]["order_id"].
3. Validation Gates (1-6) must pass before state transition:
   - HMAC signature verified.
   - Event ID present.
   - Payment status == 'captured'.
   - Correlation consistent (no case/order conflict).
   - Exact amount match (paise).
   - Exact currency match.
4. Atomic state transition:
   - status = RESOLVED_SUCCESS
   - next_run_at = None (disarms background scheduler)
   - decision_audit_trail["recovery_completion"] updated
   - ExecutionLog(action="PAYMENT_RECOVERED", status="SUCCESS") created with deterministic idempotency key.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import ExecutionMode, ExecutionStatus, RecoveryStatus
from app.models.execution_log import ExecutionLog
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.schemas.webhook import WebhookResponse
from app.services.payment_normalizer import NormalizedPaymentEvent

logger = logging.getLogger(__name__)


def _find_case_by_recovery_order_id(db: Session, order_id: str) -> RecoveryCase | None:
    """Find a candidate RecoveryCase whose decision_audit_trail contains the recovery order_id.

    To avoid full-table scan on large datasets, candidate cases are searched
    among active / unresolved cases first.
    """
    candidates = db.execute(
        select(RecoveryCase).where(
            RecoveryCase.status.in_([
                RecoveryStatus.PENDING_EXECUTION.value,
                RecoveryStatus.REQUIRES_HUMAN.value,
                RecoveryStatus.RECEIVED.value,
                RecoveryStatus.DECISION_PENDING.value,
                RecoveryStatus.EXECUTING.value,
                RecoveryStatus.RESOLVED_FAILED.value,
            ])
        )
    ).scalars().all()

    for rc in candidates:
        trail = rc.decision_audit_trail or {}
        rec_order = trail.get("recovery_order", {})
        if rec_order.get("order_id") == order_id:
            return rc

    return None


def resolve_recovery_by_payment(
    db: Session,
    normalized: NormalizedPaymentEvent,
) -> WebhookResponse:
    """Validate payment.captured event and resolve the correlated RecoveryCase to RESOLVED_SUCCESS.

    Args:
        db: Active SQLAlchemy database session.
        normalized: Normalized payment.captured data.

    Returns:
        WebhookResponse with acceptance, duplicate status, and recovery_case_id.

    Raises:
        HTTPException 400: Security or validation gate violation (amount/currency mismatch, consistency conflict).
        HTTPException 500: Database transition error.
    """
    event_id = normalized.external_event_id
    notes = normalized.notes or {}
    payment_order_id = normalized.external_order_id
    payment_id = normalized.external_payment_id

    # -----------------------------------------------------------------------
    # Step 1: Correlation Lookup
    # -----------------------------------------------------------------------
    matched_case: RecoveryCase | None = None
    notes_case_id_str = notes.get("recovery_case_id")

    # Primary correlation: notes.recovery_case_id
    if notes_case_id_str:
        try:
            rc_uuid = uuid.UUID(str(notes_case_id_str))
            matched_case = db.get(RecoveryCase, rc_uuid)
        except (ValueError, TypeError):
            logger.warning(
                "Invalid recovery_case_id UUID in payment notes: %s",
                notes_case_id_str,
            )
            matched_case = None

    # Secondary correlation: original failed order_id match
    if matched_case is None and payment_order_id:
        pe = db.execute(
            select(PaymentEvent).where(
                PaymentEvent.external_order_id == payment_order_id
            )
        ).scalar_one_or_none()

        if pe and pe.recovery_case:
            matched_case = pe.recovery_case

    # Tertiary correlation: recovery_order in decision_audit_trail
    if matched_case is None and payment_order_id:
        matched_case = _find_case_by_recovery_order_id(db, payment_order_id)

    # If no case correlates, acknowledge safely without state changes (unrelated order)
    if matched_case is None:
        logger.info(
            "Payment captured event %s (order_id=%s, payment_id=%s) does not match any RecoverAI case — acknowledged safely",
            event_id,
            payment_order_id,
            payment_id,
        )
        return WebhookResponse(
            accepted=True,
            duplicate=False,
            event_id=event_id,
            recovery_case_id=None,
            message="Payment captured acknowledged (unrelated order)",
        )

    # -----------------------------------------------------------------------
    # Step 2: Consistency & Security Checks
    # -----------------------------------------------------------------------
    # If notes specify case A, verify that order_id (if present on case) doesn't conflict with case B
    if notes_case_id_str and payment_order_id:
        # Check if the payment_order_id belongs to a DIFFERENT case's original payment event
        conflicting_pe = db.execute(
            select(PaymentEvent).where(
                PaymentEvent.external_order_id == payment_order_id
            )
        ).scalar_one_or_none()

        if (
            conflicting_pe
            and conflicting_pe.recovery_case
            and conflicting_pe.recovery_case.id != matched_case.id
        ):
            logger.error(
                "Security rejection: notes.recovery_case_id (%s) conflicts with order_id (%s) belonging to case (%s)",
                matched_case.id,
                payment_order_id,
                conflicting_pe.recovery_case.id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Correlation conflict: recovery_case_id and order_id mismatch",
            )

    # -----------------------------------------------------------------------
    # Step 3: Validation Gates (Status, Amount, Currency)
    # -----------------------------------------------------------------------
    # Gate 3: Payment status must be 'captured' (if status is provided in payload)
    if normalized.status and normalized.status != "captured":
        logger.warning(
            "Payment status '%s' is not 'captured' for event %s",
            normalized.status,
            event_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment status must be 'captured', got '{normalized.status}'",
        )

    # Gate 5: Amount exact match
    expected_amount = matched_case.payment_event.amount_paise
    if normalized.amount_paise != expected_amount:
        logger.error(
            "Amount mismatch rejection: incoming amount %d paise != expected %d paise (case %s)",
            normalized.amount_paise,
            expected_amount,
            matched_case.id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment amount mismatch: expected {expected_amount} paise, got {normalized.amount_paise} paise",
        )

    # Gate 6: Currency exact match
    expected_currency = matched_case.payment_event.currency.upper()
    if normalized.currency.upper() != expected_currency:
        logger.error(
            "Currency mismatch rejection: incoming currency '%s' != expected '%s' (case %s)",
            normalized.currency,
            expected_currency,
            matched_case.id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment currency mismatch: expected '{expected_currency}', got '{normalized.currency}'",
        )

    # -----------------------------------------------------------------------
    # Step 4: Lock Row & Atomic State Transition
    # -----------------------------------------------------------------------
    locked_case = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == matched_case.id)
        .with_for_update()
    ).scalar_one_or_none()

    if locked_case is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to acquire lock on target recovery case",
        )

    # Idempotency check: If already RESOLVED_SUCCESS, return idempotent 200 OK
    if locked_case.status == RecoveryStatus.RESOLVED_SUCCESS.value:
        logger.info(
            "Case %s already RESOLVED_SUCCESS — acknowledging duplicate payment.captured event %s",
            locked_case.id,
            event_id,
        )
        return WebhookResponse(
            accepted=True,
            duplicate=True,
            event_id=event_id,
            recovery_case_id=str(locked_case.id),
            message="Case already resolved as SUCCESS",
        )

    previous_status = locked_case.status
    now_utc = datetime.now(timezone.utc)

    # Transition state to RESOLVED_SUCCESS and cancel all future scheduler runs
    locked_case.status = RecoveryStatus.RESOLVED_SUCCESS.value
    locked_case.next_run_at = None

    # Update decision_audit_trail with recovery completion metadata
    trail = dict(locked_case.decision_audit_trail or {})
    trail["recovery_completion"] = {
        "payment_id": payment_id,
        "order_id": payment_order_id,
        "amount_paise": normalized.amount_paise,
        "currency": normalized.currency,
        "recovered_at": now_utc.isoformat(),
        "webhook_event_id": event_id,
        "previous_status": previous_status,
    }
    locked_case.decision_audit_trail = trail

    # Create immutable ExecutionLog record
    idempotency_key = f"recovery_success:{locked_case.id}:{payment_id or event_id}"
    log = ExecutionLog(
        recovery_case_id=locked_case.id,
        idempotency_key=idempotency_key,
        action="PAYMENT_RECOVERED",
        execution_mode=ExecutionMode.SIMULATION.value,
        status=ExecutionStatus.SUCCESS.value,
        request_data={
            "event_type": normalized.event_type,
            "external_event_id": event_id,
            "payment_id": payment_id,
            "order_id": payment_order_id,
            "amount_paise": normalized.amount_paise,
            "currency": normalized.currency,
            "notes": notes,
        },
        response_data={
            "resolved": True,
            "previous_status": previous_status,
            "new_status": RecoveryStatus.RESOLVED_SUCCESS.value,
            "payment_id": payment_id,
            "recovered_amount_paise": normalized.amount_paise,
        },
        error_message=None,
        executed_at=now_utc,
    )
    db.add(log)

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.warning(
            "Concurrent duplicate execution log insertion for %s: %s",
            idempotency_key,
            e,
        )
        return WebhookResponse(
            accepted=True,
            duplicate=True,
            event_id=event_id,
            recovery_case_id=str(locked_case.id),
            message="Duplicate resolution acknowledged (race)",
        )

    logger.info(
        "Successfully resolved RecoveryCase %s from %s to RESOLVED_SUCCESS (payment_id=%s, amount=%d paise)",
        locked_case.id,
        previous_status,
        payment_id,
        normalized.amount_paise,
    )

    return WebhookResponse(
        accepted=True,
        duplicate=False,
        event_id=event_id,
        recovery_case_id=str(locked_case.id),
        message="Payment recovery verified and case resolved successfully",
    )
