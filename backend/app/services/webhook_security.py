"""Razorpay webhook signature verification."""

import hashlib
import hmac
from typing import NamedTuple


class VerificationResult(NamedTuple):
    """Result of signature verification."""

    valid: bool
    reason: str


def verify_razorpay_signature(
    raw_body: bytes,
    signature: str,
    webhook_secret: str,
) -> VerificationResult:
    """Verify the Razorpay webhook signature using HMAC-SHA256.

    Args:
        raw_body: The raw HTTP request body bytes (not parsed).
        signature: The value from the X-Razorpay-Signature header.
        webhook_secret: The configured RAZORPAY_WEBHOOK_SECRET.

    Returns:
        VerificationResult indicating success or failure with reason.
    """
    if not webhook_secret:
        return VerificationResult(
            valid=False,
            reason="Webhook secret not configured",
        )

    if not signature:
        return VerificationResult(
            valid=False,
            reason="Missing signature header",
        )

    # Compute HMAC-SHA256 over the raw body
    computed = hmac.new(
        key=webhook_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Timing-safe comparison to prevent timing attacks
    is_valid = hmac.compare_digest(computed, signature)

    if is_valid:
        return VerificationResult(valid=True, reason="Signature verified")
    else:
        return VerificationResult(valid=False, reason="Invalid signature")
