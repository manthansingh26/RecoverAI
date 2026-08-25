"""Razorpay webhook ingestion endpoint.

Receives raw webhook requests, verifies HMAC-SHA256 signature,
persists payment.failed events with recovery case creation,
and resolves recovery cases on verified payment.captured events.
"""

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.metrics import metrics
from app.db.session import get_db
from app.models.enums import RecoveryStatus
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.schemas.webhook import WebhookResponse
from app.services.ingestion_service import ingest_payment_event
from app.services.payment_normalizer import (
    normalize_payment_captured,
    normalize_payment_failed,
)
from app.services.recovery_executor import execute_single_case
from app.services.recovery_resolver import resolve_recovery_by_payment
from app.services.recovery_workflow import process_received_case
from app.services.webhook_security import verify_razorpay_signature

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_event_created_at(payload: dict[str, Any]) -> float | None:
    """Extract the TOP-LEVEL webhook event ``created_at`` (Unix seconds).

    Razorpay's standard event envelope carries ``created_at`` at the top level
    of the event object (not ``payload.payment.entity.created_at``). This is
    the timestamp used for replay/freshness protection.

    Returns ``None`` when the field is missing or malformed — callers treat a
    missing value as "no freshness signal" and accept the event. This is the
    compatibility policy: real Razorpay events always include it, but trimmed
    test payloads and older fixtures may omit it, and we must not reject them.
    Future timestamps (clock skew) are accepted because the computed age is
    negative, never stale.
    """
    raw = payload.get("created_at")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Malformed webhook created_at=%r — ignoring freshness", raw)
        return None
    if value <= 0:
        return None
    return value


def _event_is_stale(payload: dict[str, Any]) -> bool:
    """True when the top-level ``created_at`` is older than the max event age.

    Implements Razorpay's documented replay rule: reject events where
    ``created_at`` is more than ``WEBHOOK_MAX_EVENT_AGE_SECONDS`` in the past.
    """
    created = _extract_event_created_at(payload)
    if created is None:
        return False
    return time.time() - created > settings.WEBHOOK_MAX_EVENT_AGE_SECONDS


def _payment_event_exists(db: Session, event_id: str) -> bool:
    """True if a PaymentEvent with this ``external_event_id`` already exists.

    Uses the SAME column ingestion deduplicates on
    (``uq_payment_events_external_event_id``) — not a new lookup mechanism.
    Distinguishes a stale delivery retry (known event-id → acknowledge as a
    duplicate) from a novel stale event (unknown event-id → replay, ignore).
    """
    return (
        db.execute(
            select(PaymentEvent.id).where(PaymentEvent.external_event_id == event_id)
        ).scalar()
        is not None
    )


async def _read_webhook_body(request: Request, max_bytes: int) -> bytes:
    """Read a webhook request body while enforcing a maximum size.

    Two-layer guard that applies BEFORE the payload is fully buffered into
    application memory:

    1. Content-Length fast path — if the client declares a Content-Length
       above the limit we reject immediately (413) without reading the body.

    2. Streaming cap — the body is read incrementally via ``request.stream()``
       and rejected as soon as the accumulated size exceeds the limit. This
       covers chunked transfer, missing Content-Length, and clients that lie
       about Content-Length.

    The returned bytes are the exact raw payload, byte-for-byte identical to
    what ``await request.body()`` would return, so HMAC verification semantics
    are preserved (signature is computed over these raw bytes).

    Note on honesty: this caps what the *application* buffers into Python
    memory. The ASGI server (e.g. uvicorn) may still read the full wire body
    into its own receive buffer before our handler runs, so this is
    application-level protection — not a wire-level streaming guarantee.
    """
    # 1. Content-Length fast path
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            declared_length = int(content_length_header)
        except ValueError:
            # Malformed Content-Length — fall through to the streaming cap.
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Webhook payload exceeds maximum size of {max_bytes} bytes",
            )

    # 2. Streaming cap (authoritative guard)
    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Webhook payload exceeds maximum size of {max_bytes} bytes",
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _process_case_if_received(db: Session, recovery_case_id: str) -> None:
    """Run the decision engine if a case is still stuck in RECEIVED.

    Self-healing for the window where a prior webhook delivery committed the
    event during ingestion but failed before decision-engine processing
    completed — leaving the case stranded in RECEIVED and making Razorpay's
    retry a no-op (duplicate ingestion skips the pipeline).

    process_received_case is idempotent: it locks the row and skips any case
    not in RECEIVED, so calling it here is safe for both fresh and duplicate
    deliveries. Failures are swallowed so a self-heal attempt can never break
    webhook acknowledgement.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        logger.warning(
            "Invalid recovery_case_id during self-heal: %s", recovery_case_id
        )
        return

    try:
        rc = db.get(RecoveryCase, rc_uuid)
        if rc is not None and rc.status == RecoveryStatus.RECEIVED.value:
            logger.info(
                "Re-processing case %s stranded in RECEIVED state",
                recovery_case_id,
            )
            process_received_case(db, recovery_case_id)
    except Exception as e:
        logger.warning(
            "Self-heal re-processing failed for case %s: %s",
            recovery_case_id,
            e,
        )


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
    4. Process payment.failed (ingest & start recovery pipeline).
    5. Process payment.captured (canonical recovery resolution).
    6. Acknowledge order.paid and other events safely without state change.

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
    # 1. Read raw body bytes BEFORE any parsing, with a size cap so an
    #    arbitrarily large payload is never fully buffered into memory.
    metrics.increment("webhook_received")
    _request_started = time.perf_counter()
    try:
        return await _process_webhook(request, x_razorpay_signature, x_razorpay_event_id, db)
    finally:
        metrics.add("webhook_processing_seconds_total", time.perf_counter() - _request_started)


async def _process_webhook(
    request: Request,
    x_razorpay_signature: str,
    x_razorpay_event_id: str,
    db: Session,
) -> WebhookResponse:
    """Inner webhook handler (split out so latency can be timed)."""
    raw_body = await _read_webhook_body(
        request, settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES
    )

    # 2. Verify signature against raw body
    verification = verify_razorpay_signature(
        raw_body=raw_body,
        signature=x_razorpay_signature,
        webhook_secret=settings.RAZORPAY_WEBHOOK_SECRET,
    )

    if not verification.valid:
        metrics.increment("webhook_rejected_hmac")
        logger.warning(
            "Webhook signature verification failed: %s", verification.reason
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )
    metrics.increment("webhook_verified")

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
        metrics.increment("webhook_malformed")
        logger.warning("Failed to parse webhook body as JSON: %s", e)
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON in request body",
        )

    event_type = body_json.get("event", "")

    # --- 5. Replay / freshness protection (Milestone 15A, Design B) ----------
    # Razorpay's documented replay rule: reject events whose top-level
    # created_at is more than WEBHOOK_MAX_EVENT_AGE_SECONDS in the past.
    # A stale event MUST be acknowledged with HTTP 200 (never 4xx) so Razorpay
    # stops retrying it. Missing/malformed created_at => no freshness signal =>
    # accept (compatibility policy).
    #
    # Scoping (Design B):
    #   - payment.failed: a KNOWN event-id is a delivery retry, not a replay —
    #     it falls through to ingest_payment_event(), which acknowledges it as
    #     a duplicate idempotently. A NOVEL stale event-id would otherwise
    #     create a duplicate recovery case, so it is ignored.
    #   - payment.captured: freshness is enforced inside the resolver AFTER its
    #     RESOLVED_SUCCESS idempotency check, so retries of an already-resolved
    #     case are still acknowledged while a stale capture never mutates a case.
    #   - order.paid / other events: acknowledged without state change;
    #     freshness is irrelevant.
    if event_type == "payment.failed" and _event_is_stale(body_json):
        if not _payment_event_exists(db, x_razorpay_event_id):
            metrics.increment("webhook_rejected_stale")
            logger.warning(
                "Ignoring stale payment.failed event %s (created_at=%s)",
                x_razorpay_event_id,
                body_json.get("created_at"),
            )
            return WebhookResponse(
                accepted=True,
                stale=True,
                event_id=x_razorpay_event_id,
                recovery_case_id=None,
                message="Stale webhook event ignored",
            )

    # 6. Canonical Recovery Resolution: payment.captured
    if event_type == "payment.captured":
        try:
            normalized_captured = normalize_payment_captured(
                event_id=x_razorpay_event_id,
                payload_data=body_json,
                raw_payload=body_json,
            )
        except ValueError as e:
            logger.warning("payment.captured normalization failed: %s", e)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid payment.captured payload: {e}",
            )

        return resolve_recovery_by_payment(
            db=db,
            normalized=normalized_captured,
            stale=_event_is_stale(body_json),
        )

    # 6. Order Paid: acknowledged safely without independent financial state transition
    if event_type == "order.paid":
        logger.info(
            "Acknowledging order.paid event %s (canonical recovery resolution handled via payment.captured)",
            x_razorpay_event_id,
        )
        return WebhookResponse(
            accepted=True,
            duplicate=False,
            event_id=x_razorpay_event_id,
            recovery_case_id=None,
            message="Event type 'order.paid' acknowledged (canonical resolution handled by payment.captured)",
        )

    # 7. Non-failure / other events: acknowledged safely
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

    # 8. Normalize the payment.failed payload
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

    # 9. Persist with idempotency
    result = ingest_payment_event(
        db=db,
        normalized=normalized,
        source="razorpay_webhook",
        signature_verified=True,
    )

    if result.duplicate:
        metrics.increment("webhook_duplicate")

    if not result.success:
        logger.error("Ingestion failed: %s", result.message)
        raise HTTPException(
            status_code=500,
            detail="Internal error processing webhook",
        )

    # 10. Ensure the case advances through the recovery intelligence pipeline.
    #     A previous delivery may have committed the event during ingestion but
    #     failed before running the decision engine, stranding the case in
    #     RECEIVED. _process_case_if_received is idempotent and re-processes
    #     such cases even on duplicate deliveries (self-healing).
    if result.recovery_case_id:
        _process_case_if_received(db, result.recovery_case_id)

        # Auto-execute newly-processed PENDING_EXECUTION cases in SIMULATION
        # mode. Development convenience only — runs for cases this request
        # actually created (not on duplicate retries, to avoid re-executing).
        if not result.duplicate:
            try:
                rc_uuid = uuid.UUID(result.recovery_case_id)
                rc = db.get(RecoveryCase, rc_uuid)
                if (
                    rc is not None
                    and rc.status == RecoveryStatus.PENDING_EXECUTION.value
                    and settings.EXECUTION_MODE == "SIMULATION"
                ):
                    execute_single_case(db, result.recovery_case_id)
            except Exception as e:
                logger.warning(
                    "Auto-execution check failed for case %s: %s",
                    result.recovery_case_id,
                    e,
                )

    logger.info(
        "Webhook ingested and processed: event_id=%s duplicate=%s recovery_case_id=%s",
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
