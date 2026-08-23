"""API routes for payment order creation and checkout integration."""

import logging

from fastapi import APIRouter, status

from app.schemas.payment import CreateOrderRequest, CreateOrderResponse
from app.services.payment_service import create_razorpay_order_internal

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
    1. Converts input amount to integer paise.
    2. Calls payment_service to create the order with Razorpay.
    3. Returns only public/safe fields needed by frontend checkout (never exposes KEY_SECRET).

    Args:
        payload: CreateOrderRequest containing amount and optional receipt/notes.

    Returns:
        CreateOrderResponse containing key_id, order_id, amount (in paise), currency, and receipt.
    """
    # Convert amount to integer paise
    if payload.amount_in_rupees:
        amount_paise = int(round(payload.amount * 100))
    else:
        amount_paise = int(round(payload.amount))

    result = create_razorpay_order_internal(
        amount_paise=amount_paise,
        currency=payload.currency,
        receipt=payload.receipt,
        notes=payload.notes,
    )

    return CreateOrderResponse(
        key_id=result.key_id,
        order_id=result.order_id,
        amount=result.amount_paise,
        currency=result.currency,
        receipt=result.receipt,
    )
