"""API routes for payment order creation and checkout integration."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
import razorpay

from app.core.config import settings
from app.schemas.payment import CreateOrderRequest, CreateOrderResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.post(
    "/create-order",
    response_model=CreateOrderResponse,
    status_code=status.HTTP_200_OK,
    summary="Create a Razorpay Test Mode Order",
)
def create_razorpay_order(payload: CreateOrderRequest) -> CreateOrderResponse:
    """Create a new order in Razorpay for frontend Checkout in Test Mode.

    Flow:
    1. Validates configured Razorpay credentials.
    2. Converts input amount to integer paise.
    3. Calls Razorpay API via the official Python SDK.
    4. Returns only public/safe fields needed by frontend checkout (never exposes KEY_SECRET).

    Args:
        payload: CreateOrderRequest containing amount and optional receipt/notes.

    Returns:
        CreateOrderResponse containing key_id, order_id, amount (in paise), currency, and receipt.

    Raises:
        HTTPException 503: Razorpay credentials not configured in environment.
        HTTPException 400: Unsupported currency, invalid amount, or Razorpay validation error.
        HTTPException 502: Razorpay gateway failure.
        HTTPException 500: Unexpected internal server error.
    """
    key_id = settings.RAZORPAY_KEY_ID
    key_secret = settings.RAZORPAY_KEY_SECRET

    if not key_id or not key_secret:
        logger.error("Razorpay credentials not configured in backend environment")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay payment gateway is not configured on the server. Please set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in backend/.env",
        )

    # Validate currency
    currency = payload.currency.upper()
    if currency != "INR":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported currency '{payload.currency}'. Only 'INR' is supported.",
        )

    # Convert amount to integer paise
    if payload.amount_in_rupees:
        amount_paise = int(round(payload.amount * 100))
    else:
        amount_paise = int(round(payload.amount))

    if amount_paise < 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amount must be at least ₹1.00 (100 paise), got {amount_paise} paise",
        )

    receipt = payload.receipt or f"rcpt_{uuid.uuid4().hex[:12]}"
    order_data: dict[str, Any] = {
        "amount": amount_paise,
        "currency": currency,
        "receipt": receipt,
        "payment_capture": 1,
    }
    if payload.notes:
        order_data["notes"] = payload.notes

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

    logger.info("Created Razorpay Test order: order_id=%s, amount=%d paise", order_id, amount_paise)

    return CreateOrderResponse(
        key_id=key_id,
        order_id=order_id,
        amount=amount_paise,
        currency=currency,
        receipt=receipt,
    )
