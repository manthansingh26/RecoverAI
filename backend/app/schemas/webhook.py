"""Pydantic schemas for Razorpay webhook payloads."""

from pydantic import BaseModel, Field


class RazorpayPaymentPayload(BaseModel):
    """Razorpay payment.failed event payload (partial, fields we care about)."""

    entity: str | None = None
    event: str | None = None
    account_id: str | None = None
    created_at: int | None = None

    class PayloadPayment(BaseModel):
        """Payment object inside the payload."""

        id: str | None = None
        entity: str | None = None
        amount: int | None = None
        currency: str | None = None
        status: str | None = None
        order_id: str | None = None
        invoice_id: str | None = None
        international: bool | None = None
        method: str | None = None
        amount_refunded: int | None = None
        refund_status: str | None = None
        captured: bool | None = None
        description: str | None = None
        card_id: str | None = None
        bank: str | None = None
        wallet: str | None = None
        vpa: str | None = None
        email: str | None = None
        contact: str | None = None
        notes: dict | None = None
        error_code: str | None = None
        error_description: str | None = None
        error_source: str | None = None
        error_step: str | None = None
        error_reason: str | None = None

    payload: PayloadPayment | None = None


class WebhookResponse(BaseModel):
    """Standard webhook ingestion response."""

    accepted: bool
    duplicate: bool = False
    event_id: str | None = None
    recovery_case_id: str | None = None
    message: str | None = None


class SimulationRequest(BaseModel):
    """Request body for the development simulation endpoint."""

    event_id: str | None = None
    payment_id: str | None = None
    order_id: str | None = None
    amount_paise: int = Field(default=100000, ge=1)
    currency: str = "INR"
    error_code: str | None = " payment_failed"
    error_reason: str | None = "account_expired"
    error_description: str | None = "The payment has failed."
