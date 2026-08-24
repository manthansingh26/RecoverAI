"""Recovery Review Service — human approval/rejection and dashboard operations.

Provides:
- List recovery cases with filtering and pagination
- Get case detail with payment event and execution history
- Approve a case requiring human review
- Reject a case requiring human review
- Dashboard summary metrics
- Manual safe execution trigger

All operations are idempotent and concurrency-safe where applicable.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, cast, func, select, Date
from sqlalchemy.orm import Session, joinedload

import copy

from app.models.enums import (
    ExecutionStatus,
    FailureCategory,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.execution_log import ExecutionLog
from app.models.payment_event import PaymentEvent
from app.models.recovery_case import RecoveryCase
from app.services.failure_classifier import classify_failure
from app.services.policy_engine import evaluate_policy
from app.services.strategy_advisor import recommend_strategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ReviewActionResult:
    """Result of an approval or rejection action."""

    recovery_case_id: str
    previous_status: str
    new_status: str
    previous_approved_by_human: bool | None
    new_approved_by_human: bool | None
    action: str
    message: str
    resolved_strategy: str | None = None


# ---------------------------------------------------------------------------
# List recovery cases
# ---------------------------------------------------------------------------

def list_recovery_cases(
    db: Session,
    *,
    status: str | None = None,
    strategy: str | None = None,
    requires_human_approval: bool | None = None,
    approved_by_human: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[RecoveryCase], int]:
    """List recovery cases with optional filters and pagination.

    Returns:
        (cases, total_count)
    """
    stmt = select(RecoveryCase)

    if status is not None:
        stmt = stmt.where(RecoveryCase.status == status)
    if strategy is not None:
        stmt = stmt.where(RecoveryCase.recommended_strategy == strategy)
    if requires_human_approval is not None:
        stmt = stmt.where(RecoveryCase.requires_human_approval == requires_human_approval)
    if approved_by_human is not None:
        stmt = stmt.where(RecoveryCase.approved_by_human == approved_by_human)

    # Get total count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_stmt).scalar() or 0

    # Apply ordering and pagination
    stmt = stmt.order_by(RecoveryCase.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    cases = list(db.execute(stmt).scalars().all())
    return cases, total


# ---------------------------------------------------------------------------
# Get case detail
# ---------------------------------------------------------------------------

def get_case_detail(
    db: Session,
    recovery_case_id: str,
) -> RecoveryCase | None:
    """Get a RecoveryCase by ID with relationships loaded.

    Returns:
        RecoveryCase or None if not found.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return None

    rc = db.get(RecoveryCase, rc_uuid)
    return rc


def get_execution_logs(
    db: Session,
    recovery_case_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ExecutionLog], int]:
    """Get execution logs for a case with pagination.

    Returns:
        (logs, total_count)
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return [], 0

    stmt = select(ExecutionLog).where(
        ExecutionLog.recovery_case_id == rc_uuid
    )

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_stmt).scalar() or 0

    # Order newest first, paginate
    stmt = stmt.order_by(ExecutionLog.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    logs = list(db.execute(stmt).scalars().all())
    return logs, total


# ---------------------------------------------------------------------------
# Post-Approval Strategy Resolution
# ---------------------------------------------------------------------------


def _resolve_post_approval_strategy(
    db: Session,
    rc: RecoveryCase,
) -> str | None:
    """Resolve a safe executable strategy after human approval.

    Called when a HUMAN_REVIEW case is approved. Re-derives strategy using:
    1. The existing failure category from the RecoveryCase (already set by
       the decision engine during ingestion).
    2. The strategy advisor to get a candidate strategy.
    3. The policy engine to validate the candidate.
    4. If the policy still forces HUMAN_REVIEW, falls back to
       CREATE_PAYMENT_LINK (customer-initiated, lowest automation risk).

    The audit trail records:
    - original_strategy: what the decision engine originally recommended
    - resolved_strategy: what was selected post-approval
    - resolution_reason: why this strategy was chosen
    - policy_validation: result of re-running the policy engine

    Returns:
        Strategy value string if resolution succeeded, None if the case
        has no payment event or resolution is impossible.
    """
    payment_event = rc.payment_event
    if payment_event is None:
        logger.warning(
            "Cannot resolve strategy: RecoveryCase %s has no PaymentEvent", rc.id
        )
        return None

    # 1. Use existing classification from the RecoveryCase
    try:
        failure_category = FailureCategory(rc.failure_category)
    except (ValueError, TypeError):
        logger.warning(
            "Cannot resolve strategy: invalid failure_category '%s'",
            rc.failure_category,
        )
        return None

    # 2. Get strategy recommendation from the advisor
    recommendation = recommend_strategy(
        category=failure_category,
        retry_count=rc.retry_count,
        amount_paise=payment_event.amount_paise,
    )

    # 3. Validate through policy engine (human_approved=True since this
    #    is a post-approval resolution — the human has already reviewed).
    policy_decision = evaluate_policy(
        amount_paise=payment_event.amount_paise,
        failure_category=failure_category,
        proposed_strategy=recommendation.strategy,
        recovery_probability=recommendation.confidence,
        retry_count=rc.retry_count,
        human_approved=True,
    )

    candidate_strategy = policy_decision.final_strategy

    # 4. If the resolved strategy is still HUMAN_REVIEW or STOP_RECOVERY,
    #    fall back to CREATE_PAYMENT_LINK — it is customer-initiated and
    #    carries the lowest automation risk.
    if candidate_strategy in (
        RecoveryStrategy.HUMAN_REVIEW,
        RecoveryStrategy.STOP_RECOVERY,
    ):
        # Re-validate CREATE_PAYMENT_LINK through policy (human_approved=True)
        fallback_policy = evaluate_policy(
            amount_paise=payment_event.amount_paise,
            failure_category=failure_category,
            proposed_strategy=RecoveryStrategy.CREATE_PAYMENT_LINK,
            recovery_probability=recommendation.confidence,
            retry_count=rc.retry_count,
            human_approved=True,
        )
        if fallback_policy.approved or fallback_policy.final_strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value:
            candidate_strategy = RecoveryStrategy.CREATE_PAYMENT_LINK
        else:
            # Policy still blocks — cannot resolve
            logger.warning(
                "Policy blocks CREATE_PAYMENT_LINK for case %s: %s",
                rc.id, fallback_policy.violations,
            )
            return None

    # Only allow executable strategies through
    if candidate_strategy not in (
        RecoveryStrategy.WAIT_AND_RETRY,
        RecoveryStrategy.CREATE_PAYMENT_LINK,
    ):
        logger.warning(
            "Resolved strategy %s is not executable for case %s",
            candidate_strategy.value, rc.id,
        )
        return None

    # 5. Record resolution in the audit trail
    trail = copy.deepcopy(rc.decision_audit_trail or {})
    trail["approval_resolution"] = {
        "original_strategy": rc.recommended_strategy,
        "resolved_strategy": candidate_strategy.value,
        "resolution_reason": (
            f"Human approved HUMAN_REVIEW case. Strategy advisor recommended "
            f"{recommendation.strategy.value} for {failure_category.value} "
            f"category. Policy approved {candidate_strategy.value}."
        ),
        "policy_validation": {
            "approved": policy_decision.approved,
            "final_strategy": policy_decision.final_strategy.value,
            "violations": policy_decision.violations,
        },
        "failure_category": failure_category.value,
        "advisor_confidence": recommendation.confidence,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    rc.decision_audit_trail = trail

    return candidate_strategy.value


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def approve_case(
    db: Session,
    recovery_case_id: str,
    *,
    actor: str | None = None,
) -> ReviewActionResult | None:
    """Approve a recovery case requiring human review.

    Idempotent: repeated approval does not corrupt state.
    Concurrency-safe: uses row locking.

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase.
        actor: Operator identifier for audit attribution (e.g. operator email
               or "system:scheduler"). If None, uses "unknown".

    Returns:
        ReviewActionResult or None if case not found.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return None

    # Lock the row
    rc = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == rc_uuid)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if rc is None:
        return None

    previous_status = rc.status
    previous_approved = rc.approved_by_human

    # If already approved, return idempotent result
    if rc.approved_by_human is True:
        return ReviewActionResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            previous_approved_by_human=previous_approved,
            new_approved_by_human=rc.approved_by_human,
            action="approved",
            message="Case was already approved",
        )

    # If rejected, cannot re-approve
    if rc.approved_by_human is False:
        return ReviewActionResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            previous_approved_by_human=previous_approved,
            new_approved_by_human=rc.approved_by_human,
            action="approval_failed",
            message="Case was previously rejected and cannot be re-approved",
        )

    # Perform approval
    rc.approved_by_human = True

    # Audit attribution — record who approved this case (Milestone 14A).
    trail = copy.deepcopy(rc.decision_audit_trail or {})
    trail["approved_by"] = {
        "actor": actor or "unknown",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    rc.decision_audit_trail = trail

    # Record original strategy before any resolution
    original_strategy = rc.recommended_strategy
    resolved_strategy: str | None = None

    if rc.status == RecoveryStatus.REQUIRES_HUMAN.value:
        if rc.recommended_strategy == RecoveryStrategy.HUMAN_REVIEW.value:
            # --- Post-approval strategy resolution ---
            # The case has HUMAN_REVIEW as its strategy. Resolve to a safe
            # executable strategy by re-running the strategy advisor on the
            # existing failure category, then validating through the policy
            # engine.
            resolved_strategy = _resolve_post_approval_strategy(
                db=db,
                rc=rc,
            )

            if resolved_strategy is not None:
                rc.recommended_strategy = resolved_strategy
                # Derive status from the resolved strategy
                rc.status = RecoveryStatus.PENDING_EXECUTION.value
                rc.next_run_at = datetime.now(timezone.utc)
            else:
                # Resolution failed — keep HUMAN_REVIEW, no transition
                logger.warning(
                    "Strategy resolution failed for case %s; "
                    "keeping HUMAN_REVIEW strategy",
                    recovery_case_id,
                )

        elif rc.recommended_strategy in (
            RecoveryStrategy.WAIT_AND_RETRY.value,
            RecoveryStrategy.CREATE_PAYMENT_LINK.value,
        ):
            # Already has an executable strategy — transition directly
            rc.status = RecoveryStatus.PENDING_EXECUTION.value
            rc.next_run_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(rc)

    # Build resolution message
    if resolved_strategy is not None:
        message = (
            f"Case approved: {previous_status} -> {rc.status}. "
            f"Strategy resolved from {original_strategy} to {resolved_strategy}."
        )
    else:
        message = f"Case approved: {previous_status} -> {rc.status}"

    logger.info(
        "Case %s approved: %s -> %s (original_strategy=%s resolved=%s)",
        recovery_case_id, previous_status, rc.status,
        original_strategy, resolved_strategy,
    )

    return ReviewActionResult(
        recovery_case_id=recovery_case_id,
        previous_status=previous_status,
        new_status=rc.status,
        previous_approved_by_human=previous_approved,
        new_approved_by_human=rc.approved_by_human,
        action="approved",
        message=message,
        resolved_strategy=resolved_strategy,
    )


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

def reject_case(
    db: Session,
    recovery_case_id: str,
    *,
    actor: str | None = None,
) -> ReviewActionResult | None:
    """Reject a recovery case requiring human review.

    Idempotent: repeated rejection does not corrupt state.
    A rejected case can never be auto-executed.

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase.
        actor: Operator identifier for audit attribution. If None, "unknown".

    Returns:
        ReviewActionResult or None if case not found.
    """
    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        return None

    # Lock the row
    rc = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == rc_uuid)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if rc is None:
        return None

    previous_status = rc.status
    previous_approved = rc.approved_by_human

    # If already rejected, return idempotent result
    if rc.approved_by_human is False:
        return ReviewActionResult(
            recovery_case_id=recovery_case_id,
            previous_status=previous_status,
            new_status=rc.status,
            previous_approved_by_human=previous_approved,
            new_approved_by_human=rc.approved_by_human,
            action="rejected",
            message="Case was already rejected",
        )

    # Perform rejection
    rc.approved_by_human = False

    # Audit attribution — record who rejected this case (Milestone 14A).
    trail = copy.deepcopy(rc.decision_audit_trail or {})
    trail["rejected_by"] = {
        "actor": actor or "unknown",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    rc.decision_audit_trail = trail

    # Transition to RESOLVED_FAILED — rejected cases stop recovery
    rc.status = RecoveryStatus.RESOLVED_FAILED.value
    rc.next_run_at = None
    rc.requires_human_approval = False

    db.commit()
    db.refresh(rc)

    logger.info(
        "Case %s rejected: %s -> %s",
        recovery_case_id, previous_status, rc.status,
    )

    return ReviewActionResult(
        recovery_case_id=recovery_case_id,
        previous_status=previous_status,
        new_status=rc.status,
        previous_approved_by_human=previous_approved,
        new_approved_by_human=rc.approved_by_human,
        action="rejected",
        message=f"Case rejected: {previous_status} -> {rc.status}",
    )


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------

def get_dashboard_summary(db: Session) -> dict[str, int]:
    """Compute dashboard summary metrics from actual database data.

    Returns:
        Dict with metric names and counts.
    """
    # Case counts by status
    status_counts = {}
    for status_val in RecoveryStatus:
        count = db.execute(
            select(func.count()).select_from(RecoveryCase).where(
                RecoveryCase.status == status_val.value
            )
        ).scalar() or 0
        status_counts[status_val.value] = count

    total_cases = sum(status_counts.values())

    # Human approval metrics
    awaiting_human = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.requires_human_approval == True,  # noqa: E712
            RecoveryCase.approved_by_human.is_(None),
        )
    ).scalar() or 0

    approved_cases = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.approved_by_human == True,  # noqa: E712
        )
    ).scalar() or 0

    # Execution metrics
    total_executions = db.execute(
        select(func.count()).select_from(ExecutionLog)
    ).scalar() or 0

    execution_status_counts = {}
    for exec_status in ExecutionStatus:
        count = db.execute(
            select(func.count()).select_from(ExecutionLog).where(
                ExecutionLog.status == exec_status.value
            )
        ).scalar() or 0
        execution_status_counts[exec_status.value] = count

    return {
        "total_cases": total_cases,
        "received_cases": status_counts.get(RecoveryStatus.RECEIVED.value, 0),
        "pending_execution_cases": status_counts.get(RecoveryStatus.PENDING_EXECUTION.value, 0),
        "requires_human_cases": status_counts.get(RecoveryStatus.REQUIRES_HUMAN.value, 0),
        "resolved_success_cases": status_counts.get(RecoveryStatus.RESOLVED_SUCCESS.value, 0),
        "resolved_failed_cases": status_counts.get(RecoveryStatus.RESOLVED_FAILED.value, 0),
        "awaiting_human_review": awaiting_human,
        "approved_cases": approved_cases,
        "total_execution_attempts": total_executions,
        "successful_executions": execution_status_counts.get(ExecutionStatus.SUCCESS.value, 0),
        "failed_executions": execution_status_counts.get(ExecutionStatus.FAILED.value, 0),
        "blocked_executions": execution_status_counts.get(ExecutionStatus.BLOCKED.value, 0),
    }


# ---------------------------------------------------------------------------
# Dashboard analytics (Milestone 9A)
# ---------------------------------------------------------------------------

def get_dashboard_analytics(db: Session) -> dict[str, Any]:
    """Compute comprehensive analytics from actual database data.

    Uses efficient SQL aggregation queries — no N+1 problems.

    Returns:
        Dict matching the DashboardAnalytics schema shape.
    """
    # ----- 1. Status distribution (GROUP BY) -----
    status_rows = db.execute(
        select(
            RecoveryCase.status,
            func.count().label("cnt"),
        )
        .group_by(RecoveryCase.status)
    ).all()

    status_distribution = [
        {"status": row.status, "count": row.cnt}
        for row in status_rows
    ]

    # Build a quick lookup for performance metrics
    status_lookup: dict[str, int] = {row.status: row.cnt for row in status_rows}
    total_cases = sum(status_lookup.values())

    # ----- 2. Strategy distribution (GROUP BY) -----
    strategy_rows = db.execute(
        select(
            RecoveryCase.recommended_strategy,
            func.count().label("cnt"),
        )
        .where(RecoveryCase.recommended_strategy.is_not(None))
        .group_by(RecoveryCase.recommended_strategy)
    ).all()

    strategy_distribution = [
        {"strategy": row.recommended_strategy, "count": row.cnt}
        for row in strategy_rows
    ]

    # ----- 3. Recovery performance metrics -----
    successful_cases = status_lookup.get(RecoveryStatus.RESOLVED_SUCCESS.value, 0)
    failed_cases = status_lookup.get(RecoveryStatus.RESOLVED_FAILED.value, 0)
    human_review_cases = status_lookup.get(RecoveryStatus.REQUIRES_HUMAN.value, 0)

    # Pending = RECEIVED + DECISION_PENDING + PENDING_EXECUTION + EXECUTING
    pending_cases = sum(
        status_lookup.get(s.value, 0)
        for s in (
            RecoveryStatus.RECEIVED,
            RecoveryStatus.DECISION_PENDING,
            RecoveryStatus.PENDING_EXECUTION,
            RecoveryStatus.EXECUTING,
        )
    )

    resolved_total = successful_cases + failed_cases
    success_rate = (
        round(successful_cases / resolved_total * 100, 1)
        if resolved_total > 0
        else 0.0
    )

    performance = {
        "total_cases": total_cases,
        "successful_cases": successful_cases,
        "failed_cases": failed_cases,
        "pending_cases": pending_cases,
        "human_review_cases": human_review_cases,
        "success_rate": success_rate,
    }

    # ----- 4. Financial metrics (JOIN with PaymentEvent) -----
    # Single query: SUM amounts grouped by case status categories
    financial_rows = db.execute(
        select(
            func.coalesce(func.sum(PaymentEvent.amount_paise), 0).label("total"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RecoveryCase.status == RecoveryStatus.RESOLVED_SUCCESS.value,
                            PaymentEvent.amount_paise,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("recovered"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RecoveryCase.status.in_([
                                RecoveryStatus.RECEIVED.value,
                                RecoveryStatus.DECISION_PENDING.value,
                                RecoveryStatus.PENDING_EXECUTION.value,
                                RecoveryStatus.EXECUTING.value,
                            ]),
                            PaymentEvent.amount_paise,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pending"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RecoveryCase.status == RecoveryStatus.REQUIRES_HUMAN.value,
                            PaymentEvent.amount_paise,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("human_review"),
        )
        .select_from(RecoveryCase)
        .join(PaymentEvent, RecoveryCase.payment_event_id == PaymentEvent.id)
    ).one()

    financial = {
        "total_failed_amount_paise": int(financial_rows.total),
        "simulated_recovered_amount_paise": int(financial_rows.recovered),
        "pending_recovery_amount_paise": int(financial_rows.pending),
        "human_review_amount_paise": int(financial_rows.human_review),
    }

    # ----- 5. Human review metrics -----
    awaiting_review = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.requires_human_approval == True,  # noqa: E712
            RecoveryCase.approved_by_human.is_(None),
        )
    ).scalar() or 0

    approved = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.approved_by_human == True,  # noqa: E712
        )
    ).scalar() or 0

    rejected = db.execute(
        select(func.count()).select_from(RecoveryCase).where(
            RecoveryCase.approved_by_human == False,  # noqa: E712
        )
    ).scalar() or 0

    human_review = {
        "awaiting_review": awaiting_review,
        "approved": approved,
        "rejected": rejected,
    }

    # ----- 6. Daily activity (last 30 days) -----
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    daily_rows = db.execute(
        select(
            cast(RecoveryCase.created_at, Date).label("day"),
            func.count().label("cnt"),
        )
        .where(RecoveryCase.created_at >= thirty_days_ago)
        .group_by("day")
        .order_by("day")
    ).all()

    daily_activity = [
        {"date": row.day, "count": row.cnt}
        for row in daily_rows
    ]

    return {
        "status_distribution": status_distribution,
        "strategy_distribution": strategy_distribution,
        "performance": performance,
        "financial": financial,
        "human_review": human_review,
        "daily_activity": daily_activity,
    }


# ---------------------------------------------------------------------------
# Live Activity Feed (Milestone 9B)
# ---------------------------------------------------------------------------

def get_dashboard_activity(db: Session, limit: int = 20) -> dict[str, Any]:
    """Compute real activity feed from ExecutionLog, RecoveryCase, and PaymentEvent records.

    Derives timeline events without fabricating data or using extra tables.
    Uses eager loading (joinedload) to eliminate N+1 queries.

    Args:
        db: Active database session.
        limit: Maximum number of activity items to return (1-100).

    Returns:
        Dict matching ActivityFeed schema with items and generated_at.
    """
    activities: list[dict[str, Any]] = []

    # 1. Fetch recent execution logs with joined recovery case & payment event
    exec_logs = (
        db.execute(
            select(ExecutionLog)
            .options(
                joinedload(ExecutionLog.recovery_case).joinedload(RecoveryCase.payment_event)
            )
            .order_by(ExecutionLog.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .unique()
        .all()
    )

    for log in exec_logs:
        rc = log.recovery_case
        pe = rc.payment_event if rc else None
        action_label = (log.action or "").replace("_", " ").title()
        occurred_at = log.executed_at or log.created_at
        success_payment_id = log.response_data.get("payment_id") or (pe.external_payment_id if pe else None)

        if log.action == "PAYMENT_RECOVERED" and log.status == ExecutionStatus.SUCCESS.value:
            activity_type = "PAYMENT_RECOVERED"
            title = "Payment Recovered"
            amt_str = f" of ₹{pe.amount_paise / 100:,.2f}" if pe and pe.amount_paise else ""
            desc = f"Payment{amt_str} verified via Razorpay Test Mode (payment.captured)"
        elif log.status == ExecutionStatus.SUCCESS.value:
            activity_type = "EXECUTION_SUCCESS"
            title = "Recovery Execution Succeeded"
            desc = f"Strategy '{action_label}' executed successfully in simulation"
        elif log.status == ExecutionStatus.FAILED.value:
            activity_type = "EXECUTION_FAILED"
            title = "Recovery Execution Failed"
            error_suffix = f": {log.error_message}" if log.error_message else ""
            desc = f"Strategy '{action_label}' execution failed{error_suffix}"
        elif log.status == ExecutionStatus.BLOCKED.value:
            activity_type = "EXECUTION_BLOCKED"
            title = "Recovery Blocked"
            desc = f"Strategy '{action_label}' blocked by safety guardrails"
        else:  # PENDING
            activity_type = "EXECUTION_PENDING"
            title = "Recovery Initiated"
            desc = f"Strategy '{action_label}' scheduled in simulation mode"

        activities.append({
            "id": f"exec_{log.id}",
            "type": activity_type,
            "title": title,
            "description": desc,
            "occurred_at": occurred_at,
            "recovery_case_id": str(rc.id) if rc else None,
            "payment_id": success_payment_id,
            "status": rc.status if rc else None,
            "strategy": log.action,
            "amount_paise": pe.amount_paise if pe else None,
        })


    # 2. Fetch recent recovery cases with joined payment event
    cases = (
        db.execute(
            select(RecoveryCase)
            .options(joinedload(RecoveryCase.payment_event))
            .order_by(RecoveryCase.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .unique()
        .all()
    )

    for rc in cases:
        pe = rc.payment_event
        amount_str = f" of ₹{pe.amount_paise / 100:,.2f}" if pe and pe.amount_paise else ""

        # Case Ingestion / Creation
        activities.append({
            "id": f"case_created_{rc.id}",
            "type": "CASE_CREATED",
            "title": "Payment Failure Ingested",
            "description": f"Failure event received for payment{amount_str}",
            "occurred_at": rc.created_at,
            "recovery_case_id": str(rc.id),
            "payment_id": pe.external_payment_id if pe else None,
            "status": rc.status,
            "strategy": rc.recommended_strategy,
            "amount_paise": pe.amount_paise if pe else None,
        })

        # Strategy Assigned (if classified and recommended)
        if rc.recommended_strategy and rc.status != RecoveryStatus.RECEIVED.value:
            strat_label = rc.recommended_strategy.replace("_", " ").title()
            prob_str = f" ({int(float(rc.recovery_probability) * 100)}% recovery confidence)" if rc.recovery_probability is not None else ""
            activities.append({
                "id": f"case_strategy_{rc.id}",
                "type": "STRATEGY_ASSIGNED",
                "title": "Recovery Strategy Selected",
                "description": f"Decision engine recommended '{strat_label}'{prob_str}",
                "occurred_at": rc.updated_at or rc.created_at,
                "recovery_case_id": str(rc.id),
                "payment_id": pe.external_payment_id if pe else None,
                "status": rc.status,
                "strategy": rc.recommended_strategy,
                "amount_paise": pe.amount_paise if pe else None,
            })

        # Human Review State (if applicable)
        if rc.requires_human_approval and rc.approved_by_human is None and rc.status == RecoveryStatus.REQUIRES_HUMAN.value:
            activities.append({
                "id": f"case_review_req_{rc.id}",
                "type": "HUMAN_REVIEW_REQUIRED",
                "title": "Human Review Required",
                "description": f"High value or policy flag requires operator approval{amount_str}",
                "occurred_at": rc.updated_at or rc.created_at,
                "recovery_case_id": str(rc.id),
                "payment_id": pe.external_payment_id if pe else None,
                "status": rc.status,
                "strategy": rc.recommended_strategy,
                "amount_paise": pe.amount_paise if pe else None,
            })
        elif rc.approved_by_human is True:
            activities.append({
                "id": f"case_review_app_{rc.id}",
                "type": "HUMAN_REVIEW_APPROVED",
                "title": "Human Review Approved",
                "description": "Operator approved case for automated execution",
                "occurred_at": rc.updated_at or rc.created_at,
                "recovery_case_id": str(rc.id),
                "payment_id": pe.external_payment_id if pe else None,
                "status": rc.status,
                "strategy": rc.recommended_strategy,
                "amount_paise": pe.amount_paise if pe else None,
            })
        elif rc.approved_by_human is False:
            activities.append({
                "id": f"case_review_rej_{rc.id}",
                "type": "HUMAN_REVIEW_REJECTED",
                "title": "Human Review Rejected",
                "description": "Operator rejected recovery; case permanently stopped",
                "occurred_at": rc.updated_at or rc.created_at,
                "recovery_case_id": str(rc.id),
                "payment_id": pe.external_payment_id if pe else None,
                "status": rc.status,
                "strategy": rc.recommended_strategy,
                "amount_paise": pe.amount_paise if pe else None,
            })

    # Deduplicate by ID
    seen_ids: set[str] = set()
    unique_activities: list[dict[str, Any]] = []
    for act in activities:
        if act["id"] not in seen_ids:
            seen_ids.add(act["id"])
            unique_activities.append(act)

    # Sort globally by occurred_at descending (with ID for stable tie-breaking)
    unique_activities.sort(
        key=lambda x: (x["occurred_at"], x["id"]),
        reverse=True,
    )

    return {
        "items": unique_activities[:limit],
        "generated_at": datetime.now(timezone.utc),
    }
