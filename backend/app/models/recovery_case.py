"""RecoveryCase model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import FailureCategory, RecoveryStatus, RecoveryStrategy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecoveryCase(Base):
    """Core state-machine record tracking recovery attempts for a payment event."""

    __tablename__ = "recovery_cases"
    __table_args__ = (
        Index("ix_recovery_cases_status", "status"),
        Index("ix_recovery_cases_next_run_at", "next_run_at"),
        CheckConstraint("retry_count >= 0", name="ck_recovery_cases_retry_count_non_negative"),
        CheckConstraint(
            "(recovery_probability IS NULL) OR (recovery_probability >= 0 AND recovery_probability <= 1)",
            name="ck_recovery_cases_probability_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    payment_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment_events.id"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=RecoveryStatus.RECEIVED.value
    )
    failure_category: Mapped[str] = mapped_column(
        String(32), default=FailureCategory.UNKNOWN.value
    )
    recovery_probability: Mapped[float | None] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    priority_score: Mapped[float | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    recommended_strategy: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    expected_value_paise: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    decision_audit_trail: Mapped[dict] = mapped_column(JSONB, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by_human: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    payment_event = relationship("PaymentEvent", back_populates="recovery_case")
    execution_logs = relationship("ExecutionLog", back_populates="recovery_case")

    def __repr__(self) -> str:
        return f"<RecoveryCase id={self.id} status={self.status}>"
