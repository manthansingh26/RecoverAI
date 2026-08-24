"""AuthSession model — server-side session for an authenticated operator."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthSession(Base):
    """Server-side session record. The raw token is only ever stored in the
    HttpOnly cookie; the database stores only its SHA-256 hash.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    # index=True produces the ix_sessions_operator_id index.
    operator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operators.id"), nullable=False, index=True
    )
    # unique=True + index=True produces a single unique index on token_hash.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    operator = relationship("Operator")

    def __repr__(self) -> str:
        return f"<AuthSession id={self.id} operator_id={self.operator_id}>"