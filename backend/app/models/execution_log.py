"""ExecutionLog model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ExecutionMode, ExecutionStatus, RecoveryStrategy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionLog(Base):
    """Audit log for every execution attempt on a recovery case."""

    __tablename__ = "execution_logs"
    __table_args__ = (
        Index("ix_execution_logs_status", "status"),
        Index("ix_execution_logs_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_cases.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    action: Mapped[str] = mapped_column(String(32))
    execution_mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=ExecutionStatus.PENDING.value)
    request_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    response_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    # Relationships
    recovery_case = relationship("RecoveryCase", back_populates="execution_logs")

    def __repr__(self) -> str:
        return f"<ExecutionLog id={self.id} status={self.status}>"
