"""Pydantic schemas for the development simulation endpoint."""

from pydantic import BaseModel, Field


class SimulationRequest(BaseModel):
    """Request body for the development simulation endpoint."""

    event_id: str | None = None
    payment_id: str | None = None
    order_id: str | None = None
    amount_paise: int = Field(default=100000, ge=1)
    currency: str = "INR"
    error_code: str | None = "payment_failed"
    error_reason: str | None = "account_expired"
    error_description: str | None = "The payment has failed."
