"""Recovery Executor — safely executes recovery strategies on eligible cases.

Handles:
- WAIT_AND_RETRY: simulated retry with success/failure outcomes
- CREATE_PAYMENT_LINK: simulated link creation
- HUMAN_REVIEW: always BLOCKED (no auto-execution)
- STOP_RECOVERY: always BLOCKED (no action)

Execution modes:
- SIMULATION (default): deterministic, testable, no external calls
- RAZORPAY: gated behind config, fails closed when credentials missing

All execution creates an ExecutionLog record for auditability.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import (
    ExecutionMode,
    ExecutionStatus,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.models.execution_log import ExecutionLog
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Result of a single execution attempt."""

    recovery_case_id: str
    strategy: str
    execution_mode: str
    status: str  # ExecutionStatus value
    previous_case_status: str
    new_case_status: str
    execution_log_id: str | None
    message: str
    response_data: dict[str, Any] | None = None


@dataclass
class ExecutionSummary:
    """Aggregate result of an execution run."""

    attempted: int
    succeeded: int
    failed: int
    blocked: int
    results: list[ExecutionResult]


# ---------------------------------------------------------------------------
# Simulation behavior — deterministic, no randomness
# ---------------------------------------------------------------------------

# In simulation mode, these are the default outcomes per strategy.
# Tests can override via the SimulationBehavior dependency injection.
_SIMULATION_OUTCOMES: dict[str, str] = {
    RecoveryStrategy.WAIT_AND_RETRY.value: ExecutionStatus.SUCCESS.value,
    RecoveryStrategy.CREATE_PAYMENT_LINK.value: ExecutionStatus.SUCCESS.value,
    RecoveryStrategy.HUMAN_REVIEW.value: ExecutionStatus.BLOCKED.value,
    RecoveryStrategy.STOP_RECOVERY.value: ExecutionStatus.BLOCKED.value,
}


class SimulationBehavior:
    """Configurable simulation outcomes for deterministic testing.

    By default, WAIT_AND_RETRY and CREATE_PAYMENT_LINK succeed.
    Tests can override outcomes per strategy.
    """

    def __init__(self) -> None:
        self._outcomes: dict[str, str] = dict(_SIMULATION_OUTCOMES)

    def set_outcome(self, strategy: str, status: str) -> None:
        """Override the simulation outcome for a strategy."""
        self._outcomes[strategy] = status

    def get_outcome(self, strategy: str) -> str:
        """Get the simulation outcome for a strategy."""
        return self._outcomes.get(strategy, ExecutionStatus.FAILED.value)

    def reset(self) -> None:
        """Reset all outcomes to defaults."""
        self._outcomes = dict(_SIMULATION_OUTCOMES)


# Module-level default simulation behavior
_default_simulation = SimulationBehavior()


def get_simulation_behavior() -> SimulationBehavior:
    """Get the default simulation behavior instance."""
    return _default_simulation


# ---------------------------------------------------------------------------
# Execution mode helpers
# ---------------------------------------------------------------------------

def _resolve_execution_mode() -> ExecutionMode:
    """Resolve the configured execution mode.

    Default is SIMULATION. RAZORPAY mode requires valid credentials.
    Fails closed: if RAZORPAY mode is configured but credentials are
    missing, falls back to SIMULATION.
    """
    mode_str = settings.EXECUTION_MODE.upper()
    try:
        mode = ExecutionMode(mode_str)
    except ValueError:
        logger.warning(
            "Invalid EXECUTION_MODE '%s', falling back to SIMULATION",
            mode_str,
        )
        return ExecutionMode.SIMULATION

    if mode == ExecutionMode.RAZORPAY:
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            logger.warning(
                "RAZORPAY mode configured but credentials missing — "
                "falling back to SIMULATION"
            )
            return ExecutionMode.SIMULATION

    return mode


# ---------------------------------------------------------------------------
# Eligibility check
# ---------------------------------------------------------------------------

def _is_eligible(rc: RecoveryCase) -> tuple[bool, str]:
    """Check if a RecoveryCase is eligible for execution.

    Returns:
        (eligible, reason_if_not_eligible)
    """
    if rc.status != RecoveryStatus.PENDING_EXECUTION.value:
        return False, f"Status is {rc.status}, not PENDING_EXECUTION"

    if rc.next_run_at is None:
        return False, "next_run_at is None"

    now = datetime.now(timezone.utc)
    if rc.next_run_at > now:
        return False, f"next_run_at {rc.next_run_at} is in the future"

    if rc.requires_human_approval and not rc.approved_by_human:
        return False, "Requires human approval but not yet approved"

    if rc.recommended_strategy is None:
        return False, "No recommended_strategy set"

    return True, ""


# ---------------------------------------------------------------------------
# Idempotency key generation
# ---------------------------------------------------------------------------

def _make_idempotency_key(
    recovery_case_id: str,
    retry_count: int,
    strategy: str,
) -> str:
    """Generate a deterministic idempotency key for an execution attempt."""
    return f"exec:{recovery_case_id}:r{retry_count}:{strategy}"


# ---------------------------------------------------------------------------
# ExecutionLog creation
# ---------------------------------------------------------------------------

def _create_execution_log(
    db: Session,
    *,
    recovery_case_id: str,
    idempotency_key: str,
    action: str,
    execution_mode: str,
    status: str,
    request_data: dict[str, Any],
    response_data: dict[str, Any],
    error_message: str | None,
    executed_at: datetime,
) -> ExecutionLog | None:
    """Create an ExecutionLog record. Returns None on idempotency conflict."""
    log = ExecutionLog(
        recovery_case_id=uuid.UUID(recovery_case_id),
        idempotency_key=idempotency_key,
        action=action,
        execution_mode=execution_mode,
        status=status,
        request_data=request_data,
        response_data=response_data,
        error_message=error_message,
        executed_at=executed_at,
    )
    db.add(log)
    try:
        db.flush()
        return log
    except IntegrityError:
        db.rollback()
        logger.info("Duplicate execution for idempotency_key=%s — skipping", idempotency_key)
        return None


# ---------------------------------------------------------------------------
# Strategy-specific execution logic
# ---------------------------------------------------------------------------

def _execute_wait_and_retry(
    rc: RecoveryCase,
    mode: ExecutionMode,
    sim_behavior: SimulationBehavior,
) -> tuple[str, dict[str, Any], dict[str, Any], str | None]:
    """Execute WAIT_AND_RETRY strategy.

    Returns:
        (status, request_data, response_data, error_message)
    """
    if mode == ExecutionMode.SIMULATION:
        outcome = sim_behavior.get_outcome(RecoveryStrategy.WAIT_AND_RETRY.value)

        request_data = {
            "strategy": RecoveryStrategy.WAIT_AND_RETRY.value,
            "mode": "simulation",
            "retry_count": rc.retry_count,
            "payment_event_id": str(rc.payment_event_id),
        }

        if outcome == ExecutionStatus.SUCCESS.value:
            # Simulate successful retry scheduling
            response_data = {
                "simulated": True,
                "outcome": "retry_scheduled",
                "message": "Simulated retry attempt scheduled successfully",
            }
            return ExecutionStatus.SUCCESS.value, request_data, response_data, None
        else:
            response_data = {
                "simulated": True,
                "outcome": "retry_failed",
                "message": "Simulated retry attempt failed",
            }
            return ExecutionStatus.FAILED.value, request_data, response_data, "Simulated retry failure"

    # RAZORPAY mode — not implemented in Milestone 5
    return ExecutionStatus.BLOCKED.value, {}, {}, "Real Razorpay execution not implemented"


def _execute_create_payment_link(
    rc: RecoveryCase,
    mode: ExecutionMode,
    sim_behavior: SimulationBehavior,
) -> tuple[str, dict[str, Any], dict[str, Any], str | None]:
    """Execute CREATE_PAYMENT_LINK strategy.

    Returns:
        (status, request_data, response_data, error_message)
    """
    if mode == ExecutionMode.SIMULATION:
        outcome = sim_behavior.get_outcome(RecoveryStrategy.CREATE_PAYMENT_LINK.value)

        request_data = {
            "strategy": RecoveryStrategy.CREATE_PAYMENT_LINK.value,
            "mode": "simulation",
            "payment_event_id": str(rc.payment_event_id),
        }

        if outcome == ExecutionStatus.SUCCESS.value:
            response_data = {
                "simulated": True,
                "link_created": True,
                "payment_recovered": False,
                "message": "Simulated payment link created (not yet paid by customer)",
                "simulated_link": f"https://rzp.test/sim_{uuid.uuid4().hex[:8]}",
            }
            return ExecutionStatus.SUCCESS.value, request_data, response_data, None
        else:
            response_data = {
                "simulated": True,
                "link_created": False,
                "payment_recovered": False,
                "message": "Simulated payment link creation failed",
            }
            return ExecutionStatus.FAILED.value, request_data, response_data, "Simulated link creation failure"

    # RAZORPAY mode — not implemented in Milestone 5
    return ExecutionStatus.BLOCKED.value, {}, {}, "Real Razorpay execution not implemented"


def _execute_human_review(
    rc: RecoveryCase,
    mode: ExecutionMode,
) -> tuple[str, dict[str, Any], dict[str, Any], str | None]:
    """Execute HUMAN_REVIEW — always blocked.

    Returns:
        (status, request_data, response_data, error_message)
    """
    request_data = {
        "strategy": RecoveryStrategy.HUMAN_REVIEW.value,
        "mode": mode.value,
    }
    response_data = {
        "blocked": True,
        "reason": "HUMAN_REVIEW strategy never auto-executes",
    }
    return ExecutionStatus.BLOCKED.value, request_data, response_data, None


def _execute_stop_recovery(
    rc: RecoveryCase,
    mode: ExecutionMode,
) -> tuple[str, dict[str, Any], dict[str, Any], str | None]:
    """Execute STOP_RECOVERY — never executes financial actions.

    Returns:
        (status, request_data, response_data, error_message)
    """
    request_data = {
        "strategy": RecoveryStrategy.STOP_RECOVERY.value,
        "mode": mode.value,
    }
    response_data = {
        "blocked": True,
        "reason": "STOP_RECOVERY — no action taken",
    }
    return ExecutionStatus.BLOCKED.value, request_data, response_data, None


# ---------------------------------------------------------------------------
# Main execution functions
# ---------------------------------------------------------------------------

def execute_single_case(
    db: Session,
    recovery_case_id: str,
    *,
    sim_behavior: SimulationBehavior | None = None,
    actor: str | None = None,
) -> ExecutionResult | None:
    """Execute a single eligible RecoveryCase.

    Handles eligibility checking, strategy dispatch, ExecutionLog creation,
    and RecoveryCase state transitions.

    Args:
        db: Active database session.
        recovery_case_id: UUID string of the RecoveryCase.
        sim_behavior: Optional simulation behavior override for testing.
        actor: Optional actor identifier for audit attribution. Defaults to
               "system:scheduler" when None (scheduler/webhook path).

    Returns:
        ExecutionResult or None if case not found.
    """
    if sim_behavior is None:
        sim_behavior = get_simulation_behavior()

    if actor is None:
        actor = "system:scheduler"

    try:
        rc_uuid = uuid.UUID(recovery_case_id)
    except ValueError:
        logger.warning("Invalid recovery_case_id format: %s", recovery_case_id)
        return None

    # Lock the row for update to prevent concurrent execution
    rc = db.execute(
        select(RecoveryCase)
        .where(RecoveryCase.id == rc_uuid)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if rc is None:
        logger.warning("RecoveryCase not found: %s", recovery_case_id)
        return None

    previous_status = rc.status

    # Check eligibility
    eligible, reason = _is_eligible(rc)
    if not eligible:
        logger.info("Case %s not eligible: %s", recovery_case_id, reason)
        return ExecutionResult(
            recovery_case_id=recovery_case_id,
            strategy=rc.recommended_strategy or "NONE",
            execution_mode="N/A",
            status=ExecutionStatus.BLOCKED.value,
            previous_case_status=previous_status,
            new_case_status=rc.status,
            execution_log_id=None,
            message=f"Not eligible: {reason}",
        )

    strategy = rc.recommended_strategy
    mode = _resolve_execution_mode()

    # Generate idempotency key
    idempotency_key = _make_idempotency_key(
        recovery_case_id, rc.retry_count, strategy
    )

    now = datetime.now(timezone.utc)

    # Dispatch to strategy-specific handler
    if strategy == RecoveryStrategy.WAIT_AND_RETRY.value:
        exec_status, req_data, resp_data, error_msg = _execute_wait_and_retry(
            rc, mode, sim_behavior
        )
    elif strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value:
        exec_status, req_data, resp_data, error_msg = _execute_create_payment_link(
            rc, mode, sim_behavior
        )
    elif strategy == RecoveryStrategy.HUMAN_REVIEW.value:
        exec_status, req_data, resp_data, error_msg = _execute_human_review(rc, mode)
    elif strategy == RecoveryStrategy.STOP_RECOVERY.value:
        exec_status, req_data, resp_data, error_msg = _execute_stop_recovery(rc, mode)
    else:
        exec_status = ExecutionStatus.BLOCKED.value
        req_data = {"strategy": strategy, "mode": mode.value}
        resp_data = {"blocked": True, "reason": f"Unknown strategy: {strategy}"}
        error_msg = f"Unknown strategy: {strategy}"

    # Audit attribution — record who triggered this execution (Milestone 14A).
    req_data = {**req_data, "actor": actor}

    # Create ExecutionLog
    log = _create_execution_log(
        db,
        recovery_case_id=recovery_case_id,
        idempotency_key=idempotency_key,
        action=strategy,
        execution_mode=mode.value,
        status=exec_status,
        request_data=req_data,
        response_data=resp_data,
        error_message=error_msg,
        executed_at=now,
    )

    # If duplicate (idempotency conflict), skip state transition
    if log is None:
        return ExecutionResult(
            recovery_case_id=recovery_case_id,
            strategy=strategy,
            execution_mode=mode.value,
            status=ExecutionStatus.BLOCKED.value,
            previous_case_status=previous_status,
            new_case_status=rc.status,
            execution_log_id=None,
            message="Duplicate execution — skipped (idempotency)",
        )

    # Transition RecoveryCase state based on execution outcome
    new_status = previous_status
    if exec_status == ExecutionStatus.SUCCESS.value:
        if strategy == RecoveryStrategy.WAIT_AND_RETRY.value:
            # Successful retry: schedule next attempt (increment retry_count)
            rc.retry_count += 1
            from datetime import timedelta
            rc.next_run_at = datetime.now(timezone.utc) + timedelta(
                seconds=settings.RECOVERY_RETRY_DELAY_SECONDS
            )
            new_status = RecoveryStatus.PENDING_EXECUTION.value
            rc.status = new_status
        elif strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value:
            # Link created: move to PENDING_EXECUTION (awaiting customer action)
            # Mark that link was sent — customer may or may not pay
            rc.status = RecoveryStatus.PENDING_EXECUTION.value
            new_status = rc.status
            # Set a longer wait for customer to complete payment
            from datetime import timedelta
            rc.next_run_at = datetime.now(timezone.utc) + timedelta(hours=24)
    elif exec_status == ExecutionStatus.FAILED.value:
        if strategy == RecoveryStrategy.WAIT_AND_RETRY.value:
            # Retry failed: check if we can try again
            from app.core.config import settings as cfg
            if rc.retry_count >= cfg.RECOVERY_MAX_RETRIES:
                rc.status = RecoveryStatus.RESOLVED_FAILED.value
                rc.next_run_at = None
                new_status = rc.status
            else:
                # Schedule another retry
                from datetime import timedelta
                rc.retry_count += 1
                rc.next_run_at = datetime.now(timezone.utc) + timedelta(
                    seconds=cfg.RECOVERY_RETRY_DELAY_SECONDS
                )
                new_status = RecoveryStatus.PENDING_EXECUTION.value
                rc.status = new_status
        elif strategy == RecoveryStrategy.CREATE_PAYMENT_LINK.value:
            # Link creation failed: resolve as failed
            rc.status = RecoveryStatus.RESOLVED_FAILED.value
            rc.next_run_at = None
            new_status = rc.status
    # BLOCKED: no state transition

    db.commit()
    db.refresh(rc)

    logger.info(
        "Execution for %s: strategy=%s mode=%s status=%s",
        recovery_case_id, strategy, mode.value, exec_status,
    )

    return ExecutionResult(
        recovery_case_id=recovery_case_id,
        strategy=strategy,
        execution_mode=mode.value,
        status=exec_status,
        previous_case_status=previous_status,
        new_case_status=new_status,
        execution_log_id=str(log.id),
        message=f"Executed {strategy} in {mode.value} mode: {exec_status}",
        response_data=resp_data,
    )


def execute_due_cases(
    db: Session,
    *,
    sim_behavior: SimulationBehavior | None = None,
    actor: str | None = None,
) -> ExecutionSummary:
    """Execute all eligible PENDING_EXECUTION cases.

    Finds due cases (next_run_at <= now), then processes each through
    the execution engine.

    Args:
        db: Active database session.
        sim_behavior: Optional simulation behavior override for testing.
        actor: Optional actor identifier for audit attribution. Defaults to
               "system:scheduler" (each execute_single_case applies its own
               default when None).

    Returns:
        ExecutionSummary with counts and per-case results.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(RecoveryCase)
        .where(
            RecoveryCase.status == RecoveryStatus.PENDING_EXECUTION.value,
            RecoveryCase.next_run_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    due_cases = list(db.execute(stmt).scalars().all())

    results: list[ExecutionResult] = []
    for rc in due_cases:
        result = execute_single_case(
            db, str(rc.id), sim_behavior=sim_behavior, actor=actor
        )
        if result is not None:
            results.append(result)

    attempted = sum(1 for r in results if r.status != ExecutionStatus.BLOCKED.value)
    succeeded = sum(1 for r in results if r.status == ExecutionStatus.SUCCESS.value)
    failed = sum(1 for r in results if r.status == ExecutionStatus.FAILED.value)
    blocked = sum(1 for r in results if r.status == ExecutionStatus.BLOCKED.value)

    logger.info(
        "Execution run: attempted=%d succeeded=%d failed=%d blocked=%d",
        attempted, succeeded, failed, blocked,
    )

    return ExecutionSummary(
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        blocked=blocked,
        results=results,
    )
