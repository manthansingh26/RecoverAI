"""Pydantic schemas for payment operations and order creation."""

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    """Request schema to create a Razorpay Test Mode checkout order."""

    amount: float = Field(
        ...,
        gt=0,
        le=1000000,
        description="Amount in INR (Rupees by default, or paise if amount_in_rupees=False).",
    )
    amount_in_rupees: bool = Field(
        default=True,
        description="Whether the amount field is in rupees (default: True) or in paise (False).",
    )
    currency: str = Field(
        default="INR",
        description="3-letter currency code. Currently 'INR' is supported.",
    )
    receipt: str | None = Field(
        default=None,
        max_length=40,
        description="Optional merchant receipt identifier.",
    )
    notes: dict[str, str] | None = Field(
        default=None,
        description="Optional key-value metadata attached to the order.",
    )


class CreateOrderResponse(BaseModel):
    """Safe response schema returned to the frontend for Razorpay Checkout."""

    key_id: str = Field(..., description="Razorpay Key ID for client-side checkout.")
    order_id: str = Field(..., description="Razorpay order ID (e.g. order_...).")
    amount: int = Field(..., description="Amount in integer paise required by Razorpay checkout.")
    currency: str = Field(default="INR", description="Currency code.")
    receipt: str | None = Field(default=None, description="Receipt identifier.")
