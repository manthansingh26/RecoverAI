"""Ingestion service: persist normalized payment events and create recovery cases.

Uses database-level uniqueness constraints for race-safe idempotency.
On duplicate external_event_id, safely returns the existing record without
creating a duplicate.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import FailureCategory, RecoveryStatus
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.services.payment_normalizer import NormalizedPaymentEvent


@dataclass
class IngestionResult:
    """Result of an ingestion attempt."""

    success: bool
    duplicate: bool
    payment_event_id: str | None
    recovery_case_id: str | None
    message: str


def ingest_payment_event(
    db: Session,
    normalized: NormalizedPaymentEvent,
) -> IngestionResult:
    """Persist a normalized payment event and create its recovery case.

    Uses database-level unique constraint on external_event_id for
    race-safe idempotency. On IntegrityError, the transaction is
    rolled back and the existing record is returned.

    Args:
        db: Active SQLAlchemy session.
        normalized: Validated and normalized payment event data.

    Returns:
        IngestionResult with success/duplicate status and IDs.
    """
    # Check if event already exists (fast path)
    existing = db.execute(
        select(PaymentEvent).where(
            PaymentEvent.external_event_id == normalized.external_event_id
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Find the existing recovery case if any
        rc = db.execute(
            select(RecoveryCase).where(
                RecoveryCase.payment_event_id == existing.id
            )
        ).scalar_one_or_none()

        return IngestionResult(
            success=True,
            duplicate=True,
            payment_event_id=str(existing.id),
            recovery_case_id=str(rc.id) if rc else None,
            message="Duplicate event acknowledged",
        )

    # Create new PaymentEvent
    payment_event = PaymentEvent(
        event_type=normalized.event_type,
        external_event_id=normalized.external_event_id,
        external_payment_id=normalized.external_payment_id,
        external_order_id=normalized.external_order_id,
        amount_paise=normalized.amount_paise,
        currency=normalized.currency,
        error_code=normalized.error_code,
        error_reason=normalized.error_reason,
        error_description=normalized.error_description,
        raw_payload=normalized.raw_payload,
        payload_hash=normalized.payload_hash,
    )

    db.add(payment_event)

    try:
        db.flush()  # Assign payment_event.id without committing
    except IntegrityError:
        db.rollback()
        # Concurrent duplicate — fetch the existing one
        existing = db.execute(
            select(PaymentEvent).where(
                PaymentEvent.external_event_id == normalized.external_event_id
            )
        ).scalar_one_or_none()

        if existing is not None:
            rc = db.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == existing.id
                )
            ).scalar_one_or_none()
            return IngestionResult(
                success=True,
                duplicate=True,
                payment_event_id=str(existing.id),
                recovery_case_id=str(rc.id) if rc else None,
                message="Duplicate event acknowledged (race)",
            )
        # This should not happen, but handle gracefully
        return IngestionResult(
            success=False,
            duplicate=False,
            payment_event_id=None,
            recovery_case_id=None,
            message="Failed to persist payment event",
        )

    # Create RecoveryCase
    recovery_case = RecoveryCase(
        payment_event_id=payment_event.id,
        status=RecoveryStatus.RECEIVED.value,
        failure_category=FailureCategory.UNKNOWN.value,
        recovery_probability=None,
        priority_score=None,
        recommended_strategy=None,
        expected_value_paise=None,
        decision_audit_trail={
            "ingestion": {
                "source": "razorpay_webhook",
                "event_id": normalized.external_event_id,
                "signature_verified": True,
            }
        },
        retry_count=0,
        requires_human_approval=False,
        approved_by_human=None,
    )

    db.add(recovery_case)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # PaymentEvent exists but RecoveryCase constraint failed — unlikely but safe
        return IngestionResult(
            success=False,
            duplicate=False,
            payment_event_id=str(payment_event.id),
            recovery_case_id=None,
            message="Failed to create recovery case",
        )

    db.commit()

    return IngestionResult(
        success=True,
        duplicate=False,
        payment_event_id=str(payment_event.id),
        recovery_case_id=str(recovery_case.id),
        message="Event ingested successfully",
    )


def ingest_simulation_event(
    db: Session,
    normalized: NormalizedPaymentEvent,
) -> IngestionResult:
    """Persist a simulated payment event using the same core persistence logic.

    Identical to ingest_payment_event but sets ingestion source to
    'simulation' in the audit trail.

    Args:
        db: Active SQLAlchemy session.
        normalized: Validated and normalized payment event data.

    Returns:
        IngestionResult with success/duplicate status and IDs.
    """
    # Check if event already exists
    existing = db.execute(
        select(PaymentEvent).where(
            PaymentEvent.external_event_id == normalized.external_event_id
        )
    ).scalar_one_or_none()

    if existing is not None:
        rc = db.execute(
            select(RecoveryCase).where(
                RecoveryCase.payment_event_id == existing.id
            )
        ).scalar_one_or_none()

        return IngestionResult(
            success=True,
            duplicate=True,
            payment_event_id=str(existing.id),
            recovery_case_id=str(rc.id) if rc else None,
            message="Duplicate simulation event acknowledged",
        )

    # Create new PaymentEvent
    payment_event = PaymentEvent(
        event_type=normalized.event_type,
        external_event_id=normalized.external_event_id,
        external_payment_id=normalized.external_payment_id,
        external_order_id=normalized.external_order_id,
        amount_paise=normalized.amount_paise,
        currency=normalized.currency,
        error_code=normalized.error_code,
        error_reason=normalized.error_reason,
        error_description=normalized.error_description,
        raw_payload=normalized.raw_payload,
        payload_hash=normalized.payload_hash,
    )

    db.add(payment_event)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(PaymentEvent).where(
                PaymentEvent.external_event_id == normalized.external_event_id
            )
        ).scalar_one_or_none()

        if existing is not None:
            rc = db.execute(
                select(RecoveryCase).where(
                    RecoveryCase.payment_event_id == existing.id
                )
            ).scalar_one_or_none()
            return IngestionResult(
                success=True,
                duplicate=True,
                payment_event_id=str(existing.id),
                recovery_case_id=str(rc.id) if rc else None,
                message="Duplicate simulation event acknowledged (race)",
            )
        return IngestionResult(
            success=False,
            duplicate=False,
            payment_event_id=None,
            recovery_case_id=None,
            message="Failed to persist simulation event",
        )

    # Create RecoveryCase with simulation source in audit trail
    recovery_case = RecoveryCase(
        payment_event_id=payment_event.id,
        status=RecoveryStatus.RECEIVED.value,
        failure_category=FailureCategory.UNKNOWN.value,
        recovery_probability=None,
        priority_score=None,
        recommended_strategy=None,
        expected_value_paise=None,
        decision_audit_trail={
            "ingestion": {
                "source": "simulation",
                "event_id": normalized.external_event_id,
                "signature_verified": False,
            }
        },
        retry_count=0,
        requires_human_approval=False,
        approved_by_human=None,
    )

    db.add(recovery_case)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return IngestionResult(
            success=False,
            duplicate=False,
            payment_event_id=str(payment_event.id),
            recovery_case_id=None,
            message="Failed to create recovery case for simulation",
        )

    db.commit()

    return IngestionResult(
        success=True,
        duplicate=False,
        payment_event_id=str(payment_event.id),
        recovery_case_id=str(recovery_case.id),
        message="Simulation event ingested successfully",
    )
