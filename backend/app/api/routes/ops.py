"""Operational routes: lightweight metrics and stuck-case diagnostics.

Milestone 15B:
- GET /metrics               — in-process operational counters (JSON only).
- GET /api/ops/stuck-cases   — read-only diagnostic of cases stuck in
                               RECEIVED / REQUIRES_HUMAN / overdue
                               PENDING_EXECUTION (OPERATOR+ required).

Security posture:
- /metrics exposes aggregate COUNTS only (no secrets, tokens, payloads, PII).
- /api/ops/stuck-cases requires a valid operator session with an OPERATOR+
  role, and never mutates state.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.metrics import grouped_snapshot
from app.db.session import get_db
from app.models.enums import RecoveryStatus
from app.models.operator import Operator
from app.models.recovery_case import RecoveryCase
from app.api.deps import PermissionDenied, get_current_operator

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# /metrics — lightweight in-process counters
# ---------------------------------------------------------------------------

@router.get("/metrics")
async def metrics_endpoint() -> dict:
    """Return process-local operational counters grouped by subsystem.

    Process-local only; counters reset on restart. Exposed as plain JSON.
    No secrets, tokens, payloads, or PII are ever stored or returned.
    """
    return grouped_snapshot()


# ---------------------------------------------------------------------------
# /api/ops/stuck-cases — OPERATOR+ read-only diagnostics
# ---------------------------------------------------------------------------

async def require_operator_role(
    operator: Operator = Depends(get_current_operator),
) -> Operator:
    """Require an OPERATOR or ADMIN session (VIEWER is denied with 403)."""
    if operator.role not in ("OPERATOR", "ADMIN"):
        raise PermissionDenied("OPERATOR role required for operational diagnostics")
    return operator


def _serialize_case(rc: RecoveryCase, now: datetime) -> dict:
    """Flatten a RecoveryCase into a diagnostic row."""
    return {
        "recovery_case_id": str(rc.id),
        "status": rc.status,
        "age_seconds": int((now - (rc.updated_at or rc.created_at)).total_seconds()),
        "next_run_at": rc.next_run_at,
        "strategy": rc.recommended_strategy,
        "priority_score": float(rc.priority_score) if rc.priority_score is not None else None,
        "requires_human_approval": rc.requires_human_approval,
        "approved_by_human": rc.approved_by_human,
    }


@router.get("/api/ops/stuck-cases")
async def stuck_cases(
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_operator_role),
) -> dict:
    """Return read-only diagnostics for cases that appear stuck.

    Detects:
    - RECEIVED cases with no state change for >= STUCK_CASE_RECEIVED_SECONDS
      (default 1 hour).
    - REQUIRES_HUMAN cases waiting >= STUCK_CASE_HUMAN_REVIEW_SECONDS
      (default 24 hours).
    - PENDING_EXECUTION cases whose next_run_at is in the past (overdue).

    Age is measured from ``updated_at`` (last state change), which is the
    correct "how long has this been stuck" signal. The endpoint never mutates
    state and caps results at STUCK_CASE_MAX_RESULTS.
    """
    now = datetime.now(timezone.utc)
    max_results = settings.STUCK_CASE_MAX_RESULTS
    rows: list[dict] = []

    received_since = now - timedelta(seconds=settings.STUCK_CASE_RECEIVED_SECONDS)
    received_rows = db.execute(
        select(RecoveryCase)
        .where(
            RecoveryCase.status == RecoveryStatus.RECEIVED.value,
            RecoveryCase.updated_at < received_since,
        )
        .order_by(RecoveryCase.updated_at.asc())
        .limit(max_results)
    ).scalars().all()
    rows.extend(_serialize_case(rc, now) for rc in received_rows)

    human_since = now - timedelta(seconds=settings.STUCK_CASE_HUMAN_REVIEW_SECONDS)
    human_rows = db.execute(
        select(RecoveryCase)
        .where(
            RecoveryCase.status == RecoveryStatus.REQUIRES_HUMAN.value,
            RecoveryCase.updated_at < human_since,
        )
        .order_by(RecoveryCase.updated_at.asc())
        .limit(max_results)
    ).scalars().all()
    rows.extend(_serialize_case(rc, now) for rc in human_rows)

    overdue_rows = db.execute(
        select(RecoveryCase)
        .where(
            RecoveryCase.status == RecoveryStatus.PENDING_EXECUTION.value,
            RecoveryCase.next_run_at.is_not(None),
            RecoveryCase.next_run_at < now,
        )
        .order_by(RecoveryCase.next_run_at.asc())
        .limit(max_results)
    ).scalars().all()
    rows.extend(_serialize_case(rc, now) for rc in overdue_rows)

    # Apply a single cap across all categories (configurable).
    rows = rows[: max_results * 3]  # cap across categories
    rows.sort(key=lambda r: r["age_seconds"], reverse=True)
    rows = rows[:max_results]

    logger.info(
        "Stuck-case diagnostics requested by %s: %d cases returned",
        operator.email,
        len(rows),
    )

    return {
        "generated_at": now.isoformat(),
        "count": len(rows),
        "items": rows,
    }
