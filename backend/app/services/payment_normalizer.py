"""Normalize Razorpay webhook payloads into PaymentEvent-compatible data."""

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass
class NormalizedPaymentEvent:
    """Normalized data ready for PaymentEvent persistence."""

    event_type: str
    external_event_id: str
    external_payment_id: str | None
    external_order_id: str | None
    amount_paise: int
    currency: str
    error_code: str | None
    error_reason: str | None
    error_description: str | None
    raw_payload: dict[str, Any]
    payload_hash: str


def compute_payload_hash(raw_body: bytes) -> str:
    """Compute SHA-256 hash of the raw request body for audit/dedup."""
    return hashlib.sha256(raw_body).hexdigest()


def normalize_payment_failed(
    event_id: str,
    payload_data: dict[str, Any],
    raw_payload: dict[str, Any],
) -> NormalizedPaymentEvent:
    """Normalize a Razorpay payment.failed event.

    Args:
        event_id: The x-razorpay-event-id header value.
        payload_data: The parsed JSON event payload (top-level event fields).
        raw_payload: The full raw JSON body for storage.

    Returns:
        NormalizedPaymentEvent with fields mapped to PaymentEvent columns.

    Raises:
        ValueError: If essential fields are missing from the payload.
    """
    event_type = payload_data.get("event", "")
    if not event_type:
        raise ValueError("Missing 'event' field in payload")

    # Extract payment object from payload.
    # Real Razorpay webhooks nest the payment data under:
    #   payload.payment.entity
    # while the existing simulation format uses:
    #   payload.payment
    # (fields like amount, id, etc. directly on payload.payment)
    #
    # We prefer the real Razorpay structure when the "entity" key exists
    # and is a dict (matching Razorpay's published webhook schema).
    payload_section = payload_data.get("payload", {})
    if not isinstance(payload_section, dict):
        raise ValueError("Missing or invalid 'payload' in event data")

    payment_wrapper = payload_section.get("payment", {})
    if not isinstance(payment_wrapper, dict):
        raise ValueError("Missing or invalid 'payload.payment' object")

    # Real Razorpay shape: payload.payment.entity contains the actual fields
    if "entity" in payment_wrapper and isinstance(payment_wrapper["entity"], dict):
        payment_obj = payment_wrapper["entity"]
    else:
        # Simulation / existing test shape: fields directly on payload.payment
        payment_obj = payment_wrapper

    # amount_paise: Razorpay amount is in paise (smallest currency unit)
    amount = payment_obj.get("amount")
    if amount is None or not isinstance(amount, int) or amount <= 0:
        raise ValueError(f"Invalid or missing payment amount: {amount}")

    error_code = payment_obj.get("error_code")
    error_reason = payment_obj.get("error_reason")
    error_description = payment_obj.get("error_description")

    # Compute payload hash from the stored raw payload
    raw_json = __import__("json").dumps(raw_payload, sort_keys=True).encode()
    payload_hash = hashlib.sha256(raw_json).hexdigest()

    return NormalizedPaymentEvent(
        event_type=event_type,
        external_event_id=event_id,
        external_payment_id=payment_obj.get("id"),
        external_order_id=payment_obj.get("order_id"),
        amount_paise=amount,
        currency=payment_obj.get("currency", "INR"),
        error_code=error_code,
        error_reason=error_reason,
        error_description=error_description,
        raw_payload=raw_payload,
        payload_hash=payload_hash,
    )
