"""Customer model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    """Represents a customer who has experienced a failed payment."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifetime_value_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    historical_success_rate: Mapped[float | None] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    payment_events = relationship("PaymentEvent", back_populates="customer")

    def __repr__(self) -> str:
        return f"<Customer id={self.id} email={self.email}>"
