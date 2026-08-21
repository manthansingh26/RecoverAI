"""PaymentEvent model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentEvent(Base):
    """A raw payment failure event ingested from a provider."""

    __tablename__ = "payment_events"
    __table_args__ = (
        Index("ix_payment_events_external_payment_id", "external_payment_id"),
        Index("ix_payment_events_external_order_id", "external_order_id"),
        Index("ix_payment_events_payload_hash", "payload_hash"),
        UniqueConstraint(
            "external_event_id",
            name="uq_payment_events_external_event_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    external_event_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    external_payment_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    external_order_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_description: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    # Relationships
    customer = relationship("Customer", back_populates="payment_events")
    recovery_case = relationship(
        "RecoveryCase", back_populates="payment_event", uselist=False
    )

    def __repr__(self) -> str:
        return f"<PaymentEvent id={self.id} event_type={self.event_type}>"
