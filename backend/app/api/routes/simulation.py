"""Development simulation endpoint for payment.failed testing.

This endpoint is for local development and hackathon demos ONLY.
It bypasses Razorpay signature verification since it is a separate
development-only endpoint. It uses the same core ingestion logic
as the real webhook route.

NOT a replacement for the real Razorpay webhook endpoint.
"""

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.simulation import SimulationRequest
from app.schemas.webhook import WebhookResponse
from app.services.ingestion_service import ingest_payment_event
from app.services.payment_normalizer import normalize_payment_failed

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_simulation_enabled() -> bool:
    """Check if simulation endpoints are enabled for current environment."""
    return settings.APP_ENV in ("development", "test")


@router.post("/api/dev/simulate/payment-failed")
async def simulate_payment_failed(
    request: SimulationRequest,
    db: Session = Depends(get_db),
) -> WebhookResponse:
    """Simulate a payment.failed event for development/testing.

    This endpoint:
    - Only works in development or test environments.
    - Uses the same ingestion service as the real webhook.
    - Does NOT bypass or weaken the real webhook route.
    - Creates PaymentEvent + RecoveryCase using identical persistence rules.
    - Handles duplicate simulation event IDs idempotently.

    Args:
        request: Simulation payload with optional event_id and payment details.
        db: Database session dependency.

    Returns:
        WebhookResponse indicating acceptance and duplicate status.

    Raises:
        HTTPException 404: Simulation disabled outside dev/test.
        HTTPException 422: Invalid simulation request schema.
    """
    if not _is_simulation_enabled():
        raise HTTPException(
            status_code=404,
            detail="Simulation endpoint not available in this environment",
        )

    # Generate or use provided event ID
    event_id = request.event_id or f"sim_{uuid.uuid4().hex[:16]}"

    # Build a Razorpay-compatible payload structure
    simulated_payload: dict[str, Any] = {
        "entity": "event",
        "event": "payment.failed",
        "account_id": "simulated_account",
        "created_at": int(time.time()),
        "payload": {
            "payment": {
                "id": request.payment_id or f"pay_sim_{uuid.uuid4().hex[:12]}",
                "entity": "payment",
                "amount": request.amount_paise,
                "currency": request.currency,
                "status": "failed",
                "order_id": request.order_id,
                "error_code": request.error_code,
                "error_reason": request.error_reason,
                "error_description": request.error_description,
            }
        },
    }

    # Normalize using the same normalizer as the real webhook
    try:
        normalized = normalize_payment_failed(
            event_id=event_id,
            payload_data=simulated_payload,
            raw_payload=simulated_payload,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Simulation payload invalid: {e}",
        )

    # Persist using the shared ingestion function
    result = ingest_payment_event(
        db=db,
        normalized=normalized,
        source="simulation",
        signature_verified=False,
    )

    if not result.success:
        logger.error("Simulation ingestion failed: %s", result.message)
        raise HTTPException(
            status_code=500,
            detail="Internal error processing simulation",
        )

    logger.info(
        "Simulation ingested: event_id=%s duplicate=%s recovery_case_id=%s",
        event_id,
        result.duplicate,
        result.recovery_case_id,
    )

    return WebhookResponse(
        accepted=True,
        duplicate=result.duplicate,
        event_id=event_id,
        recovery_case_id=result.recovery_case_id,
        message=result.message,
    )
