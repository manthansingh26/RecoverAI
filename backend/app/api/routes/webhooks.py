"""Razorpay webhook ingestion endpoint.

Receives raw webhook requests, verifies HMAC-SHA256 signature,
and persists payment.failed events with recovery case creation.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.webhook import RazorpayPaymentPayload, WebhookResponse
from app.services.ingestion_service import ingest_payment_event
from app.services.payment_normalizer import normalize_payment_failed
from app.services.webhook_security import verify_razorpay_signature

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(alias="X-Razorpay-Signature"),
    x_razorpay_event_id: str = Header(alias="x-razorpay-event-id"),
    db: Session = Depends(get_db),
) -> WebhookResponse:
    """Handle incoming Razorpay webhook events.

    Flow:
    1. Read raw body bytes (no parsing before verification).
    2. Verify HMAC-SHA256 signature.
    3. Parse JSON only after successful verification.
    4. Process payment.failed events only; acknowledge others.
    5. Persist with idempotency via external_event_id uniqueness.

    Args:
        request: The raw FastAPI request object.
        x_razorpay_signature: The X-Razorpay-Signature header value.
        x_razorpay_event_id: The x-razorpay-event-id header value.
        db: Database session dependency.

    Returns:
        WebhookResponse indicating acceptance and duplicate status.

    Raises:
        HTTPException 401: Missing or invalid signature.
        HTTPException 400: Malformed payload or missing event ID.
    """
    # 1. Read raw body bytes BEFORE any parsing
    raw_body = await request.body()

    # 2. Verify signature against raw body
    verification = verify_razorpay_signature(
        raw_body=raw_body,
        signature=x_razorpay_signature,
        webhook_secret=settings.RAZORPAY_WEBHOOK_SECRET,
    )

    if not verification.valid:
        logger.warning(
            "Webhook signature verification failed: %s", verification.reason
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    # 3. Validate event ID
    if not x_razorpay_event_id:
        raise HTTPException(
            status_code=400,
            detail="Missing x-razorpay-event-id header",
        )

    # 4. Parse JSON body (only after signature verification)
    try:
        body_json: dict[str, Any] = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Failed to parse webhook body as JSON: %s", e)
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON in request body",
        )

    # 5. Check event type — only process payment.failed for this milestone
    event_type = body_json.get("event", "")
    if event_type != "payment.failed":
        logger.info(
            "Ignoring event type '%s' (event_id=%s)", event_type, x_razorpay_event_id
        )
        return WebhookResponse(
            accepted=True,
            duplicate=False,
            event_id=x_razorpay_event_id,
            recovery_case_id=None,
            message=f"Event type '{event_type}' accepted but not processed by this milestone",
        )

    # 6. Normalize the payment.failed payload
    try:
        normalized = normalize_payment_failed(
            event_id=x_razorpay_event_id,
            payload_data=body_json,
            raw_payload=body_json,
        )
    except ValueError as e:
        logger.warning("Payload normalization failed: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payment.failed payload: {e}",
        )

    # 7. Persist with idempotency
    result = ingest_payment_event(db=db, normalized=normalized)

    if not result.success:
        logger.error("Ingestion failed: %s", result.message)
        raise HTTPException(
            status_code=500,
            detail="Internal error processing webhook",
        )

    logger.info(
        "Webhook ingested: event_id=%s duplicate=%s recovery_case_id=%s",
        x_razorpay_event_id,
        result.duplicate,
        result.recovery_case_id,
    )

    return WebhookResponse(
        accepted=True,
        duplicate=result.duplicate,
        event_id=x_razorpay_event_id,
        recovery_case_id=result.recovery_case_id,
        message=result.message,
    )
