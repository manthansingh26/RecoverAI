"""Payment service — centralized Razorpay order creation.

Handles order creation for both generic checkout tests and targeted
recovery case checkout sessions in Razorpay Test Mode.

Security guarantees:
- Never returns or exposes RAZORPAY_KEY_SECRET.
- Validates amount > 0 and currency == 'INR'.
- Injects recovery-case metadata into Razorpay order notes for webhook correlation.
- Fails cleanly with descriptive HTTP exceptions when credentials are missing or Razorpay API errors.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
import razorpay
import razorpay.errors

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class OrderCreationResult:
    """Internal result of a Razorpay order creation."""

    key_id: str
    order_id: str
    amount_paise: int
    currency: str
    receipt: str
    notes: dict[str, Any]


def create_razorpay_order_internal(
    *,
    amount_paise: int,
    currency: str = "INR",
    receipt: str | None = None,
    notes: dict[str, Any] | None = None,
) -> OrderCreationResult:
    """Create an order directly in Razorpay using configured backend credentials.

    Args:
        amount_paise: Integer amount in paise (must be >= 100, i.e. ₹1.00).
        currency: Currency code (must be 'INR').
        receipt: Optional receipt string. Auto-generated if not provided.
        notes: Optional key-value metadata attached to the order.

    Returns:
        OrderCreationResult with safe public fields.

    Raises:
        HTTPException 503: Razorpay credentials not configured.
        HTTPException 400: Unsupported currency, invalid amount, or Razorpay validation error.
        HTTPException 502: Razorpay gateway error.
        HTTPException 500: Unexpected internal error.
    """
    key_id = settings.RAZORPAY_KEY_ID
    key_secret = settings.RAZORPAY_KEY_SECRET

    if not key_id or not key_secret:
        logger.error("Razorpay credentials not configured in backend environment")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Razorpay payment gateway is not configured on the server. "
                "Please set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in backend/.env"
            ),
        )

    # Validate currency
    norm_currency = currency.upper()
    if norm_currency != "INR":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported currency '{currency}'. Only 'INR' is supported.",
        )

    # Validate amount
    if amount_paise < 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amount must be at least ₹1.00 (100 paise), got {amount_paise} paise",
        )

    order_receipt = receipt or f"rcpt_{uuid.uuid4().hex[:12]}"
    order_notes = notes or {}

    order_data: dict[str, Any] = {
        "amount": amount_paise,
        "currency": norm_currency,
        "receipt": order_receipt,
        "payment_capture": 1,
    }
    if order_notes:
        order_data["notes"] = order_notes

    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        order = client.order.create(data=order_data)
    except razorpay.errors.BadRequestError as e:
        logger.warning("Razorpay BadRequestError creating order: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Razorpay order creation rejected: {str(e)}",
        )
    except razorpay.errors.GatewayError as e:
        logger.error("Razorpay GatewayError creating order: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Razorpay gateway error: {str(e)}",
        )
    except Exception as e:
        logger.exception("Unexpected error while calling Razorpay order.create: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create Razorpay order: {str(e)}",
        )

    order_id = order.get("id")
    if not order_id:
        logger.error("Razorpay response missing order id: %s", order)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Razorpay API returned response without order ID",
        )

    logger.info(
        "Created Razorpay Test order: order_id=%s, amount=%d paise, notes=%s",
        order_id,
        amount_paise,
        order_notes,
    )

    return OrderCreationResult(
        key_id=key_id,
        order_id=order_id,
        amount_paise=amount_paise,
        currency=norm_currency,
        receipt=order_receipt,
        notes=order_notes,
    )
